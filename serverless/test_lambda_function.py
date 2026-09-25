import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lambda_function
import watermarker

SECRET = "test-secret-0123456789"
CHAT_ID = 12345


class _NotFound(Exception):
    """Mimics botocore's ClientError for a missing object."""
    def __init__(self):
        super().__init__("Not Found")
        self.response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.deletes = []

    def download_file(self, bucket, key, path):
        if key not in self.objects:
            raise _NotFound()
        with open(path, "wb") as f:
            f.write(self.objects[key])

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as f:
            self.objects[key] = f.read()
        self.uploads.append(key)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        self.deletes.append(Key)


def _http_event(update, secret=SECRET, base64_body=False):
    body = json.dumps(update)
    if base64_body:
        body = base64.b64encode(body.encode()).decode()
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["x-telegram-bot-api-secret-token"] = secret
    return {"version": "2.0", "headers": headers, "body": body, "isBase64Encoded": base64_body}


def _text_update(text, chat_id=CHAT_ID):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def _task_event(update, secret=SECRET):
    return {lambda_function.TASK_KEY: update, "secret": secret}


class TestLambdaFunction(unittest.TestCase):

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {
            "BOT_TOKEN": "bot-token", "WEBHOOK_SECRET": SECRET,
            "STATE_BUCKET": "bucket", "ALLOWED_CHAT_IDS": "",
        })
        self.env.start()
        self.s3 = FakeS3()
        self.lambda_client = MagicMock()
        lambda_function._clients.clear()
        lambda_function._clients.update({"s3": self.s3, "lambda": self.lambda_client})
        self.work_dir = patch.object(lambda_function, "WORK_DIR", os.path.join(self.tmp, "work"))
        self.work_dir.start()
        self.context = MagicMock(invoked_function_arn="arn:aws:lambda:us-east-1:1:function:watermarker-bot")

    def tearDown(self):
        os.chdir(self.cwd)
        self.work_dir.stop()
        self.env.stop()
        lambda_function._clients.clear()
        shutil.rmtree(self.tmp)

    def _invoked_payload(self):
        kwargs = self.lambda_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["InvocationType"], "Event")
        self.assertEqual(kwargs["FunctionName"], self.context.invoked_function_arn)
        return json.loads(kwargs["Payload"])

    # --- Receiver ---

    def test_receive_rejects_missing_secret(self):
        result = lambda_function.handler(_http_event(_text_update("/help"), secret=None), self.context)
        self.assertEqual(result["statusCode"], 403)
        self.lambda_client.invoke.assert_not_called()

    def test_receive_rejects_wrong_secret(self):
        result = lambda_function.handler(_http_event(_text_update("/help"), secret="nope"), self.context)
        self.assertEqual(result["statusCode"], 403)
        self.lambda_client.invoke.assert_not_called()

    def test_receive_rejects_everything_without_configured_secret(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": ""}):
            result = lambda_function.handler(_http_event(_text_update("/help"), secret=""), self.context)
        self.assertEqual(result["statusCode"], 403)

    def test_receive_hands_update_to_async_worker(self):
        update = _text_update("/help")
        result = lambda_function.handler(_http_event(update), self.context)
        self.assertEqual(result["statusCode"], 200)
        payload = self._invoked_payload()
        self.assertEqual(payload[lambda_function.TASK_KEY], update)
        self.assertEqual(payload["secret"], SECRET)

    def test_receive_decodes_base64_body(self):
        update = _text_update("/help")
        result = lambda_function.handler(_http_event(update, base64_body=True), self.context)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._invoked_payload()[lambda_function.TASK_KEY], update)

    def test_receive_ignores_unparseable_body(self):
        event = _http_event({})
        event["body"] = "not json"
        result = lambda_function.handler(event, self.context)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.invoke.assert_not_called()

    def test_receive_ignores_updates_without_a_chat(self):
        result = lambda_function.handler(_http_event({"update_id": 1, "poll": {}}), self.context)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.invoke.assert_not_called()

    @patch("watermarker.tele.send_telegram")
    def test_receive_blocks_chats_outside_allowlist(self, mock_send):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "111, 222"}):
            result = lambda_function.handler(_http_event(_text_update("/help", chat_id=999)), self.context)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.invoke.assert_not_called()
        self.assertIn("999", mock_send.call_args[0][2])

    def test_receive_allows_chats_in_allowlist(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "111,222"}):
            lambda_function.handler(_http_event(_text_update("/help", chat_id=222)), self.context)
        self.lambda_client.invoke.assert_called_once()

    # --- Worker ---

    @patch("watermarker.handle_update")
    def test_task_rejects_missing_secret(self, mock_handle):
        result = lambda_function.handler(_task_event(_text_update("/help"), secret="wrong"), self.context)
        self.assertFalse(result["ok"])
        mock_handle.assert_not_called()

    @patch("watermarker.tele.send_telegram")
    def test_task_saves_changed_settings_to_s3(self, mock_send):
        lambda_function.handler(_task_event(_text_update("/size 0.5")), self.context)
        key = f"settings/{CHAT_ID}.json"
        self.assertEqual(self.s3.uploads, [key])
        self.assertEqual(json.loads(self.s3.objects[key])["size"], 0.5)

    @patch("watermarker.tele.send_telegram")
    def test_task_uses_settings_from_s3(self, mock_send):
        self.s3.objects[f"settings/{CHAT_ID}.json"] = json.dumps({"angle": 10}).encode()
        lambda_function.handler(_task_event(_text_update("/settings")), self.context)
        self.assertIn("- angle: 10", mock_send.call_args[0][2])
        # Nothing changed, so nothing is written back
        self.assertEqual(self.s3.uploads, [])
        self.assertEqual(self.s3.deletes, [])

    @patch("watermarker.tele.send_telegram")
    def test_task_start_deletes_settings_from_s3(self, mock_send):
        self.s3.objects[f"settings/{CHAT_ID}.json"] = json.dumps({"angle": 10}).encode()
        lambda_function.handler(_task_event(_text_update("/start")), self.context)
        self.assertEqual(self.s3.deletes, [f"settings/{CHAT_ID}.json"])
        self.assertNotIn(f"settings/{CHAT_ID}.json", self.s3.objects)

    @patch("watermarker.tele.send_telegram")
    def test_task_does_not_leak_settings_between_chats(self, mock_send):
        # Same warm container: chat A's local settings must not survive into chat B's run
        lambda_function.handler(_task_event(_text_update("/angle 10", chat_id=1)), self.context)
        lambda_function.handler(_task_event(_text_update("/settings", chat_id=1)), self.context)
        self.assertIn("- angle: 10", mock_send.call_args[0][2])
        self.s3.objects.clear()
        lambda_function.handler(_task_event(_text_update("/settings", chat_id=1)), self.context)
        self.assertIn("- angle: 45", mock_send.call_args[0][2])

    @patch("watermarker.tele.send_telegram_file")
    @patch("watermarker._download_file")
    def test_task_watermarks_photo_and_cleans_up(self, mock_download, mock_send_file):
        def fake_download(bot_token, file_id):
            path = os.path.join(watermarker.TEMP_DIR, "photo.jpg")
            Image.new("RGB", (200, 150), color="blue").save(path)
            return path
        mock_download.side_effect = fake_download
        sent = {}
        def fake_send(token, chat, path):
            with Image.open(path) as img:
                sent.update(exists=True, size=img.size)
        mock_send_file.side_effect = fake_send

        update = {"update_id": 1, "message": {"chat": {"id": CHAT_ID}, "photo": [{"file_id": "abc"}]}}
        lambda_function.handler(_task_event(update), self.context)

        self.assertEqual(sent, {"exists": True, "size": (200, 150)})
        self.assertFalse(os.path.exists(os.path.join(lambda_function.WORK_DIR, watermarker.TEMP_DIR)))

    @patch("watermarker.tele.send_telegram")
    def test_task_reports_storage_errors_to_user(self, mock_send):
        class Broken(FakeS3):
            def download_file(self, bucket, key, path):
                raise RuntimeError("S3 is down")
        lambda_function._clients["s3"] = Broken()
        result = lambda_function.handler(_task_event(_text_update("/settings")), self.context)
        self.assertTrue(result["ok"])
        self.assertIn("S3 is down", mock_send.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
