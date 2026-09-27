import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin
import lambda_function
import payments
import watermarker
from test_payments import FakeTelegram, SECRET
from test_quota import FakeDynamo, FREE_CHAT, UNLIMITED_CHAT, _ClientError

ADMIN = 900
BUYER = 111
PAYER = 777
COMMANDS = ["/admin", "/users", "/users 2", "/usage 111", "/charges 111", "/limits", "/stats",
            "/grant 111 50 goodwill", "/refund ch1", "/refund ch1 confirm"]


def _missing(bucket, key, path):
    raise _ClientError("404")


class AdminTestCase(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BOT_TOKEN": "bot-token", "WEBHOOK_SECRET": SECRET, "STATE_BUCKET": "bucket",
            "QUOTA_TABLE": "quota", "FREE_MONTHLY_IMAGES": "10", "SYSTEM_DAILY_IMAGES": "5000",
            "UNLIMITED_CHAT_IDS": str(UNLIMITED_CHAT), "ADMIN_CHAT_IDS": str(ADMIN), "ALLOWED_CHAT_IDS": "",
        })
        self.env.start()
        self.db = FakeDynamo(page_size=3)  # small pages, so every Query/Scan paginates
        lambda_function._clients.clear()
        lambda_function._clients.update({"dynamodb": self.db, "lambda": MagicMock()})
        self.telegram = FakeTelegram()
        self.requests = patch.object(payments.requests, "post", side_effect=self.telegram.post)
        self.requests.start()
        self.quota = lambda_function._quota()
        # One buyer: 10 free images used, a small pack bought, 5 of it spent
        self.quota.credit(BUYER, PAYER, "ch1", "small", 100, 200)
        self.quota.reserve(BUYER, 15)
        self.telegram.calls.clear()

    def tearDown(self):
        self.requests.stop()
        self.env.stop()
        lambda_function._clients.clear()

    def _run(self, text, chat=ADMIN):
        handled = admin.handle_command("bot-token", chat, text, self.quota, {str(ADMIN)})
        texts = self.telegram.texts()
        self.telegram.calls = [c for c in self.telegram.calls if c[0] != "sendMessage"]
        return handled, (texts[-1] if texts else None)

    def _reply(self, text):
        handled, reply = self._run(text)
        self.assertTrue(handled)
        return reply


class TestReadCommands(AdminTestCase):

    def test_admin_lists_the_commands(self):
        self.assertIn("/refund <charge id> confirm", self._reply("/admin"))

    def test_usage_of_one_chat(self):
        reply = self._reply("/usage 111")
        self.assertIn("This month: 15 images, 10 of 10 free", reply)
        self.assertIn("Bought images left: 95", reply)
        self.assertIn("Lifetime: 15 images", reply)
        self.assertIn("small: 100 images for 200 Stars, payer 777, charge ch1", reply)

    def test_usage_needs_a_chat_id(self):
        self.assertEqual(self._reply("/usage"), "Usage: /usage <chat id>")
        self.assertEqual(self._reply("/usage abc"), "Usage: /usage <chat id>")

    def test_users_most_active_first_and_paginated(self):
        self.quota.reserve(222, 3)
        self.quota.reserve(UNLIMITED_CHAT, 40)
        with patch.object(admin, "PAGE_SIZE", 2):
            first, second = self._reply("/users"), self._reply("/users 2")
        self.assertEqual(first.splitlines(), [
            "Chats 3 - page 1 of 2 (this month / free / bought left / lifetime)",
            f"{UNLIMITED_CHAT} (unlimited): 40 / 0 / 0 / 40",
            "111: 15 / 10 / 95 / 15",
            "Next: /users 2"])
        self.assertEqual(second.splitlines()[1:], ["222: 3 / 3 / 0 / 3"])

    def test_charges(self):
        self.quota.credit(BUYER, PAYER, "ch2", "large", 1000, 1000)
        reply = self._reply("/charges 111").splitlines()
        self.assertEqual(len(reply), 3)
        self.assertIn("charge ch1", reply[1] + reply[2])
        self.assertEqual(self._reply("/charges 5"), "Chat 5 hasn't bought anything.")

    def test_limits(self):
        reply = self._reply("/limits")
        self.assertIn("Free images per chat: 10 a month", reply)
        self.assertIn("5000 a day - 10 used today, 4990 left", reply)
        self.assertIn(f"Unlimited chats: {UNLIMITED_CHAT}", reply)

    def test_stats(self):
        self.quota.reserve(222, 2)
        reply = self._reply("/stats")
        self.assertIn("This month: 17 images for 2 chats", reply)
        self.assertIn("Sold: 100 images for 200 Stars (0 refunded purchases not counted)", reply)
        self.assertIn("Bought images not yet used: 95", reply)

    def test_admin_commands_use_no_allowance(self):
        before = {k: dict(v) for k, v in self.db.items.items()}
        for command in ["/admin", "/users", "/usage 111", "/charges 111", "/limits", "/stats", "/refund ch1"]:
            self._reply(command)
        self.assertEqual(self.db.items, before)


class TestGrant(AdminTestCase):

    def test_grant_adds_credits_and_is_audited(self):
        with patch("builtins.print") as mock_print:
            reply = self._reply("/grant 111 50 billing bug on 26 Sep")
        self.assertEqual(reply, "Granted 50 images to chat 111. Its balance went from 95 to 145.")
        self.assertEqual(self.quota.balance(BUYER), 145)
        audit = [str(c) for c in mock_print.call_args_list if "ADMIN AUDIT" in str(c)]
        self.assertEqual(len(audit), 1)
        for part in ("admin=900", "action=grant", "chat=111", "images=50", "reason='billing bug on 26 Sep'",
                     "balance_before=95", "balance_after=145"):
            self.assertIn(part, audit[0])

    def test_grant_rejects_bad_amounts(self):
        for text in ("/grant 111", "/grant 111 0", "/grant 111 -5", "/grant 111 lots", "/grant 111 1000000"):
            self.assertIn("Usage: /grant", self._reply(text))
        self.assertEqual(self.quota.balance(BUYER), 95)


class TestRefund(AdminTestCase):

    def _refund_calls(self):
        return [body for method, body in self.telegram.calls if method == "refundStarPayment"]

    def test_refund_shows_what_would_happen_before_doing_anything(self):
        reply = self._reply("/refund ch1")
        self.assertIn("Refunding sends 200 Stars back to user 777 and takes 100 images from chat 111", reply)
        self.assertIn("Only 95 of its 100 images are left", reply)
        self.assertIn("/refund ch1 confirm", reply)
        self.assertEqual(self._refund_calls(), [])
        self.assertEqual(self.quota.balance(BUYER), 95)

    def test_confirmed_refund_pays_the_payer_takes_the_credits_and_is_audited(self):
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        # The payer's user id, recorded at purchase: in a group it isn't the chat id
        self.assertEqual(self._refund_calls(), [{"user_id": PAYER, "telegram_payment_charge_id": "ch1"}])
        self.assertEqual(self.quota.balance(BUYER), 0)
        self.assertIn("Took 95 images from chat 111; its balance is now 0.", reply)
        self.assertIn("5 of the pack's images had already been used", reply)
        audit = [str(c) for c in mock_print.call_args_list if "ADMIN AUDIT" in str(c)]
        self.assertEqual(len(audit), 1)
        for part in ("action=refund", "charge='ch1'", "user='777'", "stars=200", "images_taken=95",
                     "balance_before=95", "balance_after=0"):
            self.assertIn(part, audit[0])
        self.assertTrue(self.quota.charge("ch1")["refunded"])
        self.assertIn("REFUNDED", self._reply("/charges 111"))

    def test_refund_is_idempotent(self):
        self._reply("/refund ch1 confirm")
        self.assertIn("already refunded", self._reply("/refund ch1 confirm"))
        self.assertIn("already refunded", self._reply("/refund ch1"))
        self.assertEqual(len(self._refund_calls()), 1)

    def test_unspent_pack_is_taken_back_whole(self):
        self.quota.credit(BUYER, PAYER, "ch2", "large", 1000, 1000)
        reply = self._reply("/refund ch2 confirm")
        self.assertIn("Took 1000 images from chat 111; its balance is now 95.", reply)
        self.assertNotIn("already been used", reply)

    def test_telegram_refusing_changes_nothing(self):
        self.telegram.refuse["refundStarPayment"] = "CHARGE_ALREADY_REFUNDED"
        with patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        self.assertEqual(reply, "Telegram refused the refund, so nothing changed: "
                                "refundStarPayment failed: CHARGE_ALREADY_REFUNDED")
        self.assertTrue(any("action=refund-failed" in str(c) for c in mock_print.call_args_list))
        self.assertEqual(self.quota.balance(BUYER), 95)
        self.assertIsNone(self.quota.charge("ch1").get("refunded"))
        self.assertNotIn("REFUNDED", self._reply("/charges 111"))
        # Nothing stayed flagged, so it can be tried again
        del self.telegram.refuse["refundStarPayment"]
        self.assertIn("Refunded 200 Stars", self._reply("/refund ch1 confirm"))

    def test_refund_whose_credit_deduction_fails_is_still_audited(self):
        with patch.object(self.quota, "deduct", side_effect=_ClientError("InternalServerError")), \
                patch("builtins.print") as mock_print:
            reply = self._reply("/refund ch1 confirm")
        self.assertIn("Refunded 200 Stars to user 777", reply)
        self.assertIn("taking the 100 images back from chat 111 failed", reply)
        audit = [str(c) for c in mock_print.call_args_list if "ADMIN AUDIT" in str(c)]
        self.assertEqual(len(audit), 1)
        self.assertIn("images_taken=0", audit[0])
        # Money moved, so it stays refunded: a retry must not refund it again
        self.assertIn("already refunded", self._reply("/refund ch1 confirm"))
        self.assertEqual(len(self._refund_calls()), 1)

    def test_unknown_charge(self):
        self.assertEqual(self._reply("/refund nope confirm"), "No purchase has charge id nope.")
        self.assertEqual(self._refund_calls(), [])


class TestGating(AdminTestCase):
    """A non-admin must not be able to tell the admin commands exist."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.work = patch.object(lambda_function, "WORK_DIR", self.tmp)
        self.work.start()
        s3 = MagicMock()
        s3.download_file.side_effect = _missing
        lambda_function._clients["s3"] = s3
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.work.stop()
        shutil.rmtree(self.tmp)
        watermarker.QUOTA = None
        watermarker.EXTRA_COMMANDS = None
        watermarker.EXTRA_HELP = ""
        super().tearDown()

    def _observe(self, chat, text):
        """Everything the bot does in reply to text: messages sent and lines logged."""
        self.telegram.calls.clear()
        update = {"update_id": 5, "message": {"chat": {"id": chat}, "text": text}}
        with patch.object(watermarker.tele, "send_telegram") as bot_send, patch("builtins.print") as log:
            lambda_function.handler({lambda_function.TASK_KEY: update, "secret": SECRET}, MagicMock())
        return list(self.telegram.calls), bot_send.call_args_list, [str(c) for c in log.call_args_list]

    def test_non_admins_get_exactly_the_unknown_command_behaviour(self):
        unknown = self._observe(BUYER, "/asdf")
        self.assertEqual(unknown, ([], [], []))
        for command in COMMANDS:
            for chat in (BUYER, UNLIMITED_CHAT):  # unlimited is not admin
                self.assertEqual(self._observe(chat, command), unknown, (chat, command))
        self.assertEqual(self.quota.balance(BUYER), 95)
        self.assertIsNone(self.quota.charge("ch1").get("refunded"))

    def test_admin_chat_gets_answers_through_the_worker(self):
        sent, _, _ = self._observe(ADMIN, "/admin")
        self.assertEqual(sent[0][0], "sendMessage")
        self.assertIn("Admin commands", sent[0][1]["text"])

    def test_no_admins_configured_means_no_admin_commands(self):
        with patch.dict(os.environ, {"ADMIN_CHAT_IDS": ""}):
            self.assertEqual(self._observe(ADMIN, "/admin"), ([], [], []))


if __name__ == "__main__":
    unittest.main()
