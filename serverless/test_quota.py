import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lambda_function
import watermarker
from quota import DailyQuota

SECRET = "test-secret-0123456789"
FREE_CHAT = 111
UNLIMITED_CHAT = 5173725149


class FakeDynamo:
    """Just enough of DynamoDB for quota.py: atomic ADD with UPDATED_NEW, and GetItem."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues):
        assert UpdateExpression.startswith("ADD images :n")
        key = Key["pk"]["S"]
        delta = int(ExpressionAttributeValues[":n"]["N"])
        with self.lock:
            item = self.items.setdefault(key, {"images": 0, "expires_at": int(ExpressionAttributeValues[":exp"]["N"])})
            item["images"] += delta
            return {"Attributes": {"images": {"N": str(item["images"])}}}

    def get_item(self, TableName, Key):
        item = self.items.get(Key["pk"]["S"])
        return {"Item": {"images": {"N": str(item["images"])}}} if item else {}

    def total(self, key):
        return self.items.get(key, {}).get("images", 0)


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


def _quota(db, clock=None, free=10, system=5000):
    return DailyQuota(db, "table", free_daily=free, system_daily=system,
                      unlimited_chat_ids={str(UNLIMITED_CHAT)},
                      now=clock or Clock(datetime(2026, 9, 26, 22, 30, tzinfo=timezone.utc)))


class TestDailyQuota(unittest.TestCase):

    def setUp(self):
        self.db = FakeDynamo()
        self.quota = _quota(self.db)

    def test_counts_images_per_chat_and_system(self):
        self.assertIsNone(self.quota.reserve(FREE_CHAT, 4))
        self.assertIsNone(self.quota.reserve(FREE_CHAT, 6))
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 10)
        self.assertEqual(self.db.total("system#2026-09-26"), 10)

    def test_personal_limit_names_the_user_quota(self):
        self.quota.reserve(FREE_CHAT, 10)
        message = self.quota.reserve(FREE_CHAT, 1)
        self.assertEqual(message, "Daily limit reached: you've used 10 of your 10 images today. "
                                  "Your allowance resets at 00:00 UTC (in 1h 30m).")
        # The refused request took nothing
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 10)
        self.assertEqual(self.db.total("system#2026-09-26"), 10)

    def test_batch_bigger_than_what_is_left_is_refused_whole(self):
        self.quota.reserve(FREE_CHAT, 7)
        message = self.quota.reserve(FREE_CHAT, 5)
        self.assertEqual(message, "That zip contains 5 images, but you have 3 of your 10 left today. "
                                  "Nothing was processed - your allowance resets at 00:00 UTC (in 1h 30m).")
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 7)
        self.assertEqual(self.db.total("system#2026-09-26"), 7)

    def test_batch_bigger_than_the_whole_allowance_says_so(self):
        # Retrying tomorrow wouldn't help, so the message doesn't suggest it
        message = self.quota.reserve(FREE_CHAT, 20)
        self.assertEqual(message, "That zip contains 20 images, which is more than your daily allowance of 10. "
                                  "Try splitting it into smaller zips.")
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 0)
        self.assertEqual(self.db.total("system#2026-09-26"), 0)

    def test_unlimited_chat_skips_personal_limit_but_not_system(self):
        quota = _quota(self.db, system=50)
        self.assertIsNone(quota.reserve(UNLIMITED_CHAT, 40))
        self.assertNotIn(f"chat#{UNLIMITED_CHAT}#2026-09-26", self.db.items)
        message = quota.reserve(UNLIMITED_CHAT, 20)
        self.assertIn("across all users", message)
        self.assertNotIn("your 10 images", message)
        self.assertEqual(message, "That zip contains 20 images, but the bot has 10 of its daily 50 left across "
                                  "all users. Nothing was processed - please try again after 00:00 UTC (in 1h 30m).")
        self.assertEqual(quota.reserve(UNLIMITED_CHAT, 60),
                         "That zip contains 60 images, which is more than the bot's daily limit of 50 across "
                         "all users. Try splitting it into smaller zips.")
        self.assertEqual(self.db.total("system#2026-09-26"), 40)

    def test_system_limit_names_the_system_quota(self):
        quota = _quota(self.db, system=12)
        quota.reserve(UNLIMITED_CHAT, 12)
        self.assertEqual(quota.reserve(FREE_CHAT, 1),
                         "The bot has reached its daily limit of 12 images across all users. "
                         "Please try again after 00:00 UTC (in 1h 30m).")
        # A free chat blocked by the system limit doesn't lose personal allowance
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 0)

    def test_messages_use_configured_limits(self):
        quota = _quota(self.db, free=3, system=7)
        quota.reserve(FREE_CHAT, 3)
        self.assertIn("used 3 of your 3 images", quota.reserve(FREE_CHAT, 1))
        quota.reserve(UNLIMITED_CHAT, 4)
        self.assertIn("daily limit of 7 images", quota.reserve(UNLIMITED_CHAT, 1))

    def test_release_refunds_both_counters(self):
        self.quota.reserve(FREE_CHAT, 10)
        self.quota.release(FREE_CHAT, 4)
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 6)
        self.assertEqual(self.db.total("system#2026-09-26"), 6)
        self.assertIsNone(self.quota.reserve(FREE_CHAT, 4))

    def test_day_rolls_over_at_utc_midnight(self):
        clock = Clock(datetime(2026, 9, 26, 23, 59, tzinfo=timezone.utc))
        quota = _quota(self.db, clock)
        quota.reserve(FREE_CHAT, 10)
        self.assertIsNotNone(quota.reserve(FREE_CHAT, 1))
        clock.when = datetime(2026, 9, 27, 0, 0, 1, tzinfo=timezone.utc)
        self.assertIsNone(quota.reserve(FREE_CHAT, 10))
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-27"), 10)

    def test_release_after_midnight_settles_the_reservation_day(self):
        # A zip reserved at 23:50 can still be running at 00:05
        clock = Clock(datetime(2026, 9, 26, 23, 50, tzinfo=timezone.utc))
        quota = _quota(self.db, clock)
        self.assertIsNone(quota.reserve(FREE_CHAT, 5))
        clock.when = datetime(2026, 9, 27, 0, 5, tzinfo=timezone.utc)
        quota.release(FREE_CHAT, 5)
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 0)
        self.assertEqual(self.db.total("system#2026-09-26"), 0)
        # The new day is untouched: no bonus allowance, no lowered system count
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-27"), 0)
        self.assertEqual(self.db.total("system#2026-09-27"), 0)
        self.assertIsNone(quota.reserve(FREE_CHAT, 10))
        self.assertIsNotNone(quota.reserve(FREE_CHAT, 1))

    def test_refunds_never_take_a_counter_below_zero(self):
        self.quota.reserve(FREE_CHAT, 2)
        self.quota.release(FREE_CHAT, 5)
        self.assertEqual(self.db.total(f"chat#{FREE_CHAT}#2026-09-26"), 0)
        self.assertEqual(self.db.total("system#2026-09-26"), 0)

    def test_failed_refund_is_logged_not_raised_and_other_refund_still_runs(self):
        class RefundFails(FakeDynamo):
            def update_item(self, **kwargs):
                key = kwargs["Key"]["pk"]["S"]
                if int(kwargs["ExpressionAttributeValues"][":n"]["N"]) < 0 and key.startswith("chat#"):
                    raise RuntimeError("ProvisionedThroughputExceededException")
                return super().update_item(**kwargs)

        db = RefundFails()
        quota = _quota(db)
        quota.reserve(FREE_CHAT, 10)
        with patch("builtins.print") as mock_print:
            message = quota.reserve(FREE_CHAT, 3)
        self.assertIn("Daily limit reached", message)
        self.assertTrue(any("quota refund failed" in str(c) for c in mock_print.call_args_list))
        # The chat refund failed, but the system refund still ran
        self.assertEqual(db.total("system#2026-09-26"), 10)

    def test_zero_free_allowance_messages(self):
        quota = _quota(self.db, free=0)
        message = "This bot has no daily image allowance for your chat."
        self.assertEqual(quota.reserve(FREE_CHAT, 1), message)
        self.assertEqual(quota.reserve(FREE_CHAT, 20), message)
        self.assertEqual(quota.check(FREE_CHAT), message)
        self.assertIsNone(quota.reserve(UNLIMITED_CHAT, 1))

    def test_zero_system_limit_message(self):
        quota = _quota(self.db, system=0)
        message = "This bot isn't processing images at the moment (its daily limit is 0)."
        self.assertEqual(quota.reserve(UNLIMITED_CHAT, 1), message)
        self.assertEqual(quota.check(FREE_CHAT), message)

    def test_rows_expire_after_their_day(self):
        self.quota.reserve(FREE_CHAT, 1)
        expires = self.db.items[f"chat#{FREE_CHAT}#2026-09-26"]["expires_at"]
        self.assertEqual(expires, int(datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()))

    def test_check_reports_only_exhausted_allowance(self):
        self.assertIsNone(self.quota.check(FREE_CHAT))
        self.quota.reserve(FREE_CHAT, 9)
        self.assertIsNone(self.quota.check(FREE_CHAT))
        self.quota.reserve(FREE_CHAT, 1)
        self.assertIn("Daily limit reached", self.quota.check(FREE_CHAT))
        self.assertIsNone(self.quota.check(UNLIMITED_CHAT))

    def test_concurrent_reservations_never_lose_counts_or_overshoot(self):
        quota = _quota(self.db, system=500)
        results = []

        def worker(chat):
            for _ in range(25):
                results.append((chat, quota.reserve(chat, 1)))

        # 40 chats x 25 attempts of 1 image, from 40 threads, against 10/chat and 500 total
        threads = [threading.Thread(target=worker, args=(1000 + i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = [chat for chat, refusal in results if refusal is None]
        self.assertEqual(len(granted), 400)  # every chat gets exactly its 10
        self.assertEqual(self.db.total("system#2026-09-26"), 400)
        for i in range(40):
            self.assertEqual(self.db.total(f"chat#{1000 + i}#2026-09-26"), 10)

    def test_concurrent_reservations_hold_the_system_cap(self):
        quota = _quota(self.db, system=100)
        results = []
        lock = threading.Lock()

        def worker():
            refusal = quota.reserve(UNLIMITED_CHAT, 7)
            with lock:
                results.append(refusal)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(None), 14)  # 14 x 7 = 98 fits under 100, a 15th wouldn't
        self.assertEqual(self.db.total("system#2026-09-26"), 98)


def _png():
    buf = BytesIO()
    Image.new('RGB', (64, 64), color='blue').save(buf, format='PNG')
    return buf.getvalue()


class TestQuotaInTheBot(unittest.TestCase):
    """The counting unit and success-only counting, through the real zip/photo code."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dirs = patch.multiple(watermarker, TEMP_DIR=os.path.join(self.tmp, "temp"),
                                   SETTINGS_DIR=os.path.join(self.tmp, "settings"),
                                   WATERMARKS_DIR=os.path.join(self.tmp, "watermarks"))
        self.dirs.start()
        watermarker.ensure_dirs()
        self.db = FakeDynamo()
        watermarker.QUOTA = _quota(self.db)
        self.sent_docs = []
        self.messages = []
        self.patches = [
            patch.object(watermarker, "_send_document",
                         side_effect=lambda t, c, p, n, caption="": self.sent_docs.append((n, caption))),
            patch.object(watermarker.tele, "send_telegram", side_effect=lambda t, c, m: self.messages.append(m)),
            patch.object(watermarker, "_active_watermark_path",
                         return_value=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sun.webp")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        watermarker.QUOTA = None
        self.dirs.stop()
        shutil.rmtree(self.tmp)

    def _zip(self, entries):
        path = os.path.join(watermarker.TEMP_DIR, "in.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return path

    def _chat_total(self, chat=FREE_CHAT):
        return self.db.total(f"chat#{chat}#2026-09-26")

    def test_zip_of_n_images_counts_n(self):
        entries = {f"{i}.png": _png() for i in range(6)}
        entries["notes.txt"] = b"not an image"
        with patch.object(watermarker, "_download_file", return_value=self._zip(entries)):
            watermarker.process_document("t", FREE_CHAT, {"file_id": "z", "file_name": "b.zip"})
        self.assertEqual(self.sent_docs[0][1], "Watermarked 6 image(s). Skipped 1 non-image file(s).")
        self.assertEqual(self._chat_total(), 6)

    def test_failed_images_are_refunded(self):
        entries = {"a.png": _png(), "b.png": _png(), "broken.jpg": b"nope"}
        with patch.object(watermarker, "_download_file", return_value=self._zip(entries)):
            watermarker.process_document("t", FREE_CHAT, {"file_id": "z", "file_name": "b.zip"})
        self.assertEqual(self._chat_total(), 2)
        self.assertEqual(self.db.total("system#2026-09-26"), 2)

    def test_undelivered_result_is_refunded(self):
        watermarker._send_document.side_effect = RuntimeError("Telegram refused the upload")
        with patch.object(watermarker, "_download_file", return_value=self._zip({"a.png": _png(), "b.png": _png()})):
            watermarker.process_document("t", FREE_CHAT, {"file_id": "z", "file_name": "b.zip"})
        self.assertEqual(self._chat_total(), 0)

    def test_zip_over_allowance_is_refused_whole(self):
        watermarker.QUOTA.reserve(FREE_CHAT, 7)
        entries = {f"{i}.png": _png() for i in range(5)}
        with patch.object(watermarker, "_download_file", return_value=self._zip(entries)), \
             patch.object(watermarker, "apply_watermark") as apply:
            watermarker.process_document("t", FREE_CHAT, {"file_id": "z", "file_name": "b.zip"})
        apply.assert_not_called()
        self.assertEqual(self.sent_docs, [])
        self.assertIn("That zip contains 5 images, but you have 3 of your 10 left today", self.messages[-1])
        self.assertEqual(self._chat_total(), 7)
        self.assertEqual(os.listdir(watermarker.TEMP_DIR), [])

    def test_photo_counts_one_and_failure_is_refunded(self):
        def download(token, file_id):
            path = os.path.join(watermarker.TEMP_DIR, f"{file_id}.jpg")
            if file_id == "good":
                Image.new('RGB', (80, 60)).save(path)
            else:
                with open(path, "wb") as f:
                    f.write(b"corrupt")
            return path

        with patch.object(watermarker, "_download_file", side_effect=download):
            watermarker.process_photo("t", FREE_CHAT, [{"file_id": "good"}])
            watermarker.process_photo("t", FREE_CHAT, [{"file_id": "bad"}])
        self.assertEqual(len(self.sent_docs), 1)
        self.assertEqual(self._chat_total(), 1)

    def test_commands_count_nothing(self):
        watermarker.process_text("t", FREE_CHAT, "/help")
        watermarker.process_text("t", FREE_CHAT, "/size 0.5")
        self.assertEqual(self.db.items, {})

    def test_no_quota_installed_means_no_counting(self):
        watermarker.QUOTA = None
        with patch.object(watermarker, "_download_file", return_value=self._zip({"a.png": _png()})):
            watermarker.process_document("t", FREE_CHAT, {"file_id": "z", "file_name": "b.zip"})
        self.assertEqual(len(self.sent_docs), 1)
        self.assertEqual(self.db.items, {})


class TestQuotaInTheLambda(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BOT_TOKEN": "bot-token", "WEBHOOK_SECRET": SECRET, "STATE_BUCKET": "bucket",
            "QUOTA_TABLE": "quota", "FREE_DAILY_IMAGES": "10", "SYSTEM_DAILY_IMAGES": "5000",
            "UNLIMITED_CHAT_IDS": str(UNLIMITED_CHAT), "ALLOWED_CHAT_IDS": "",
        })
        self.env.start()
        self.db = FakeDynamo()
        self.lambda_client = MagicMock()
        lambda_function._clients.clear()
        lambda_function._clients.update({"dynamodb": self.db, "lambda": self.lambda_client})
        self.context = MagicMock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:watermarker-bot")

    def tearDown(self):
        self.env.stop()
        lambda_function._clients.clear()

    def _event(self, message):
        body = json.dumps({"update_id": 1, "message": {"chat": {"id": message.pop("chat")}, **message}})
        return {"headers": {"x-telegram-bot-api-secret-token": SECRET}, "body": body}

    def _exhaust(self, chat):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.items[f"chat#{chat}#{day}"] = {"images": 10, "expires_at": 0}

    @patch("watermarker.tele.send_telegram")
    def test_receiver_refuses_exhausted_chat_without_starting_a_worker(self, mock_send):
        self._exhaust(FREE_CHAT)
        result = lambda_function.handler(self._event({"chat": FREE_CHAT, "photo": [{"file_id": "p"}]}), self.context)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.invoke.assert_not_called()
        self.assertIn("Daily limit reached", mock_send.call_args[0][2])

    def test_receiver_lets_commands_and_source_uploads_through(self):
        self._exhaust(FREE_CHAT)
        lambda_function.handler(self._event({"chat": FREE_CHAT, "text": "/settings"}), self.context)
        lambda_function.handler(self._event({"chat": FREE_CHAT, "caption": "/source",
                                             "document": {"file_id": "d"}}), self.context)
        self.assertEqual(self.lambda_client.invoke.call_count, 2)

    def test_receiver_lets_unlimited_chat_through(self):
        self._exhaust(UNLIMITED_CHAT)
        lambda_function.handler(self._event({"chat": UNLIMITED_CHAT, "photo": [{"file_id": "p"}]}), self.context)
        self.lambda_client.invoke.assert_called_once()

    def test_receiver_falls_back_to_worker_if_quota_check_fails(self):
        self.db.get_item = MagicMock(side_effect=RuntimeError("DynamoDB unavailable"))
        lambda_function.handler(self._event({"chat": FREE_CHAT, "photo": [{"file_id": "p"}]}), self.context)
        self.lambda_client.invoke.assert_called_once()

    @patch("watermarker.tele.send_telegram")
    def test_deprecated_allowlist_still_refuses_other_chats(self, mock_send):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "222"}), patch("builtins.print") as mock_print:
            lambda_function.handler(self._event({"chat": FREE_CHAT, "text": "/help"}), self.context)
        self.lambda_client.invoke.assert_not_called()
        self.assertIn("This bot is private", mock_send.call_args[0][2])
        self.assertTrue(any("ALLOWED_CHAT_IDS is deprecated" in str(c) for c in mock_print.call_args_list))

    def test_deprecated_allowlist_chats_are_unlimited(self):
        # Upgrading with ALLOWED_CHAT_IDS set must not throttle the owner's own chats
        self._exhaust(FREE_CHAT)
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": str(FREE_CHAT)}):
            self.assertTrue(lambda_function._quota().is_unlimited(FREE_CHAT))
            lambda_function.handler(self._event({"chat": FREE_CHAT, "photo": [{"file_id": "p"}]}), self.context)
        self.lambda_client.invoke.assert_called_once()

    def test_quota_disabled_without_a_table(self):
        with patch.dict(os.environ, {"QUOTA_TABLE": ""}):
            self.assertIsNone(lambda_function._quota())
        quota = lambda_function._quota()
        self.assertEqual((quota.free_daily, quota.system_daily, quota.unlimited), (10, 5000, {str(UNLIMITED_CHAT)}))


if __name__ == "__main__":
    unittest.main()
