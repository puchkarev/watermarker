import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lambda_function
import payments
import watermarker
from test_quota import FakeDynamo, FREE_CHAT, UNLIMITED_CHAT, _ClientError

SECRET = "test-secret-0123456789"
PAYER = 777
BALANCE = (f"chat#{FREE_CHAT}", "balance")


class FakeTelegram:
    """Records Bot API calls made through payments.requests.post."""

    def __init__(self):
        self.calls = []
        self.allowed_updates = ["message", "pre_checkout_query"]
        self.refuse = {}  # method -> Telegram's error description, or (description, error code)
        self.unreachable = set()  # methods whose call gets no answer at all

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        response = MagicMock()
        if method in self.unreachable:
            raise ConnectionError("Read timed out")
        if method in self.refuse:
            description, code = (self.refuse[method], 400) if isinstance(self.refuse[method], str) \
                else self.refuse[method]
            response.json.return_value = {"ok": False, "description": description, "error_code": code}
            return response
        result = {"allowed_updates": self.allowed_updates} if method == "getWebhookInfo" else True
        response.json.return_value = {"ok": True, "result": result}
        return response

    def methods(self):
        return [method for method, _ in self.calls]

    def texts(self):
        return [body["text"] for method, body in self.calls if method == "sendMessage"]


def _http(update):
    return {"headers": {"x-telegram-bot-api-secret-token": SECRET}, "body": json.dumps(update)}


def _pre_checkout(payload="images-v1:small", amount=200, currency="XTR"):
    return {"update_id": 1, "pre_checkout_query": {
        "id": "q1", "from": {"id": PAYER}, "currency": currency, "total_amount": amount,
        "invoice_payload": payload}}


def _paid(charge="stars_abc", payload="images-v1:small", amount=200, chat=FREE_CHAT, payer=PAYER):
    return {"update_id": 2, "message": {
        "chat": {"id": chat}, "from": {"id": payer},
        "successful_payment": {"currency": "XTR", "total_amount": amount, "invoice_payload": payload,
                               "telegram_payment_charge_id": charge}}}


class PaymentTestCase(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BOT_TOKEN": "bot-token", "WEBHOOK_SECRET": SECRET, "STATE_BUCKET": "bucket",
            "QUOTA_TABLE": "quota", "FREE_MONTHLY_IMAGES": "10", "SYSTEM_DAILY_IMAGES": "5000",
            "UNLIMITED_CHAT_IDS": str(UNLIMITED_CHAT), "ALLOWED_CHAT_IDS": "",
        })
        self.env.start()
        self.db = FakeDynamo()
        self.lambda_client = MagicMock()
        lambda_function._clients.clear()
        lambda_function._clients.update({"dynamodb": self.db, "lambda": self.lambda_client})
        self.context = MagicMock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:watermarker-bot")
        self.telegram = FakeTelegram()
        self.requests = patch.object(payments.requests, "post", side_effect=self.telegram.post)
        self.requests.start()

    def tearDown(self):
        self.requests.stop()
        self.env.stop()
        lambda_function._clients.clear()


class TestPreCheckout(PaymentTestCase):

    def _answer(self, update):
        result = lambda_function.handler(_http(update), self.context)
        self.assertEqual(result["statusCode"], 200)
        # Answered by the receiver itself: the worker could take minutes, Telegram waits 10 seconds
        self.lambda_client.invoke.assert_not_called()
        (method, body), = self.telegram.calls
        self.assertEqual(method, "answerPreCheckoutQuery")
        self.assertEqual(body["pre_checkout_query_id"], "q1")
        return body

    def test_valid_pack_is_accepted(self):
        self.assertEqual(self._answer(_pre_checkout()), {"pre_checkout_query_id": "q1", "ok": True})

    def test_unknown_pack_is_refused(self):
        body = self._answer(_pre_checkout(payload="images-v1:huge"))
        self.assertFalse(body["ok"])
        self.assertIn("no longer valid", body["error_message"])

    def test_wrong_price_is_refused(self):
        self.assertFalse(self._answer(_pre_checkout(amount=1))["ok"])

    def test_wrong_currency_is_refused(self):
        self.assertFalse(self._answer(_pre_checkout(currency="USD"))["ok"])

    def test_refused_when_purchases_cant_be_stored(self):
        with patch.dict(os.environ, {"QUOTA_TABLE": ""}):
            body = self._answer(_pre_checkout())
        self.assertFalse(body["ok"])
        self.assertIn("Nothing was charged", body["error_message"])


class TestSuccessfulPayment(PaymentTestCase):

    def _deliver(self, update):
        return lambda_function.handler(_http(update), self.context)

    def test_payment_is_credited_by_the_receiver(self):
        result = self._deliver(_paid())
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.invoke.assert_not_called()
        self.assertEqual(self.db.total(BALANCE), 100)
        self.assertEqual(self.telegram.texts(),
                         ["Thank you! 100 images were added to this chat. You now have 100 bought images."])

    def test_redelivered_payment_is_credited_once_and_thanked_once(self):
        self._deliver(_paid())
        self._deliver(_paid())
        self.assertEqual(self.db.total(BALANCE), 100)
        self.assertEqual(len(self.telegram.texts()), 1)

    def test_payer_is_recorded_for_a_group_purchase(self):
        self._deliver(_paid(chat=-100555, payer=PAYER))
        self.assertEqual(self.db.items[("charge#stars_abc", "charge")]["user_id"], str(PAYER))
        self.assertEqual(self.db.items[("charge#stars_abc", "charge")]["chat_id"], "-100555")

    def test_storage_failure_asks_telegram_to_deliver_again(self):
        def unavailable(**kwargs):
            raise _ClientError("InternalServerError")
        self.db.transact_write_items = unavailable
        with patch("builtins.print") as mock_print:
            result = self._deliver(_paid())
        self.assertEqual(result["statusCode"], 500)
        self.assertTrue(any("PAYMENT CREDIT FAILED" in str(c) for c in mock_print.call_args_list))
        del self.db.transact_write_items  # back to the working fake: the retry succeeds
        self.assertEqual(self._deliver(_paid())["statusCode"], 200)
        self.assertEqual(self.db.total(BALANCE), 100)

    def test_failed_confirmation_does_not_ask_for_redelivery(self):
        broken = MagicMock(return_value=MagicMock(json=lambda: {"ok": False, "description": "blocked"}))
        with patch.object(payments.requests, "post", broken):
            result = self._deliver(_paid())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self.db.total(BALANCE), 100)

    def test_unknown_pack_is_not_credited_and_is_flagged(self):
        with patch("builtins.print") as mock_print:
            self._deliver(_paid(payload="images-v1:huge"))
        self.assertEqual(self.db.total(BALANCE), 0)
        self.assertTrue(any("PAYMENT NOT CREDITED" in str(c) for c in mock_print.call_args_list))
        self.assertIn(payments.SUPPORT_EMAIL, self.telegram.texts()[0])

    def test_failed_notice_for_an_uncreditable_payment_does_not_loop(self):
        broken = MagicMock(return_value=MagicMock(json=lambda: {"ok": False, "description": "blocked"}))
        with patch.object(payments.requests, "post", broken), patch("builtins.print") as mock_print:
            result = self._deliver(_paid(payload="images-v1:huge"))
        # A permanent condition: answering 500 would have Telegram redeliver it forever
        self.assertEqual(result["statusCode"], 200)
        self.assertTrue(any("PAYMENT NOT CREDITED and the notice failed" in str(c) for c in mock_print.call_args_list))

    def test_payment_is_credited_even_for_a_chat_outside_the_old_allowlist(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "222"}):
            self._deliver(_paid())
        self.assertEqual(self.db.total(BALANCE), 100)


class TestCommands(PaymentTestCase):

    def setUp(self):
        super().setUp()
        self.quota = lambda_function._quota()

    def _run(self, text, chat=FREE_CHAT):
        return payments.handle_command("bot-token", chat, text, self.quota)

    def test_buy_sends_one_invoice_per_pack_in_stars(self):
        self.assertTrue(self._run("/buy"))
        invoices = [body for method, body in self.telegram.calls if method == "sendInvoice"]
        self.assertEqual([(i["payload"], i["currency"], i["prices"][0]["amount"]) for i in invoices],
                         [("images-v1:small", "XTR", 200), ("images-v1:large", "XTR", 1000)])
        self.assertTrue(all("provider_token" not in i for i in invoices))
        self.assertIn("All purchases are final", self.telegram.texts()[0])

    def test_buy_refuses_loudly_when_the_webhook_drops_pre_checkout(self):
        # An older deploy.sh set allowed_updates=["message"]: every checkout would silently fail
        self.telegram.allowed_updates = ["message"]
        with patch("builtins.print") as mock_print:
            self._run("/buy")
        self.assertNotIn("sendInvoice", self.telegram.methods())
        self.assertTrue(any("PAYMENTS BROKEN" in str(c) for c in mock_print.call_args_list))
        self.assertIn("temporarily unavailable", self.telegram.texts()[0])

    def test_unlimited_chat_has_nothing_to_buy(self):
        self._run("/buy", chat=UNLIMITED_CHAT)
        self.assertNotIn("sendInvoice", self.telegram.methods())

    def test_balance(self):
        self.quota.reserve(FREE_CHAT, 4)
        self.quota.credit(FREE_CHAT, PAYER, "c1", "small", 100, 200)
        self._run("/balance@add_sun_watermark_bot")
        text = self.telegram.texts()[0]
        self.assertIn("Free images left this month: 6 of 10", text)
        self.assertIn("Bought images: 100.", text)

    def test_usecredits_records_consent_for_today(self):
        self._run("/usecredits")
        self.assertTrue(self.quota.uses_credits_when_capped(FREE_CHAT, lambda_function._quota().now().strftime("%Y-%m-%d")))
        self.assertIn("your bought images will be used instead", self.telegram.texts()[0])
        self._run("/usecredits", chat=UNLIMITED_CHAT)
        self.assertIn("nothing to change", self.telegram.texts()[1])

    def test_terms_and_paysupport(self):
        self._run("/terms")
        self._run("/paysupport")
        terms, support = self.telegram.texts()
        self.assertIn("Your images are never retained", terms)
        self.assertIn("All purchases are final", terms)
        self.assertIn(payments.TERMS_URL, terms)
        self.assertIn(payments.SUPPORT_EMAIL, support)

    def test_other_text_is_left_to_the_bot(self):
        self.assertFalse(self._run("/size 0.5"))
        self.assertFalse(self._run("/balancesheet"))
        self.assertFalse(self._run(""))
        self.assertEqual(self.telegram.calls, [])

    def test_refund_star_payment_uses_the_payer_not_the_chat(self):
        payments.refund_star_payment("bot-token", str(PAYER), "stars_abc")
        self.assertEqual(self.telegram.calls,
                         [("refundStarPayment", {"user_id": PAYER, "telegram_payment_charge_id": "stars_abc"})])

    def test_telegram_errors_are_raised(self):
        self.telegram.post = MagicMock(return_value=MagicMock(json=lambda: {"ok": False, "description": "nope"}))
        with patch.object(payments.requests, "post", side_effect=self.telegram.post):
            with self.assertRaises(payments.TelegramError):
                payments.refund_star_payment("bot-token", PAYER, "stars_abc")


class TestWorkerWiring(PaymentTestCase):
    """The worker installs the payment commands into the shared bot code."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.work = patch.object(lambda_function, "WORK_DIR", self.tmp)
        self.work.start()
        self.s3 = MagicMock()
        self.s3.download_file.side_effect = _missing
        lambda_function._clients["s3"] = self.s3
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.work.stop()
        shutil.rmtree(self.tmp)
        watermarker.QUOTA = None
        watermarker.EXTRA_COMMANDS = None
        watermarker.EXTRA_HELP = ""
        super().tearDown()

    def _task(self, text):
        update = {"update_id": 3, "message": {"chat": {"id": FREE_CHAT}, "text": text}}
        with patch.object(watermarker.tele, "send_telegram") as bot_send:
            lambda_function.handler({lambda_function.TASK_KEY: update, "secret": SECRET}, self.context)
        return bot_send

    def test_buy_reaches_the_payment_commands(self):
        bot_send = self._task("/buy")
        self.assertIn("sendInvoice", self.telegram.methods())
        bot_send.assert_not_called()

    def test_help_lists_the_payment_commands(self):
        bot_send = self._task("/help")
        self.assertIn("/buy - Buy more images with Telegram Stars.", bot_send.call_args[0][2])


def _missing(bucket, key, path):
    raise _ClientError("404")


class TestServerDeploymentUnchanged(unittest.TestCase):

    def test_without_the_hook_payment_commands_are_not_handled(self):
        self.assertIsNone(watermarker.EXTRA_COMMANDS)
        with patch.object(watermarker.tele, "send_telegram") as send:
            watermarker.handle_update("t", {"message": {"chat": {"id": 1}, "text": "/help"}})
        self.assertNotIn("/buy", send.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
