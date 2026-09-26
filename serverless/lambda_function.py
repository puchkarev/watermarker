"""AWS Lambda entry point for the watermarker bot (on-demand, webhook-driven).

This runs the exact same bot logic as watermarker.py, but instead of an
always-on polling loop, Telegram pushes each message to a Lambda Function URL:

    Telegram --POST--> receiver (this handler, HTTP event)
                          | verifies the webhook secret, answers 200 at once
                          +--async invoke--> worker (this handler, task event)
                                               runs watermarker.handle_update()

The receiver answers immediately because Telegram re-sends an update when the
webhook is slow, which would process the same zip more than once.

The only things kept between runs are each chat's configuration (its settings
JSON and its /source watermark image, tiny objects in S3) and the daily image
counters in DynamoDB (quota.py). Photos and zips only ever live in /tmp for the
duration of one invocation.

Environment variables:
    BOT_TOKEN         Telegram bot token
    WEBHOOK_SECRET    shared secret Telegram sends in X-Telegram-Bot-Api-Secret-Token
    STATE_BUCKET      S3 bucket holding per-chat settings and watermarks
    QUOTA_TABLE       DynamoDB table of daily image counters (see quota.py); unset disables quotas
    FREE_DAILY_IMAGES     images per UTC day for chats not in UNLIMITED_CHAT_IDS (default 10)
    SYSTEM_DAILY_IMAGES   images per UTC day for the whole deployment (default 5000)
    UNLIMITED_CHAT_IDS    comma-separated chat ids with no personal limit (the system limit still applies)
    ALLOWED_CHAT_IDS  deprecated: comma-separated chat ids; when set, every other chat is refused
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# In the Lambda package the bot sits next to this file; in the repo it is one level up.
for _path in (_HERE, os.path.dirname(_HERE)):
    if os.path.exists(os.path.join(_path, "watermarker.py")) and _path not in sys.path:
        sys.path.insert(0, _path)

import watermarker
from quota import DailyQuota

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/watermarker")
TASK_KEY = "watermarker_task"

_clients = {}


def _client(name):
    """Create boto3 clients lazily so the module imports without AWS available."""
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def _response(status, body=""):
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": body}


def _secret_matches(given):
    expected = os.environ.get("WEBHOOK_SECRET", "")
    return bool(expected) and hmac.compare_digest(str(given or ""), expected)


def _id_set(variable):
    raw = os.environ.get(variable, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _allowed_chat_ids():
    return _id_set("ALLOWED_CHAT_IDS")


def _quota():
    """The deployment's daily image quota, or None when no quota table is configured."""
    table = os.environ.get("QUOTA_TABLE")
    if not table:
        return None
    return DailyQuota(_client("dynamodb"), table,
                      free_daily=os.environ.get("FREE_DAILY_IMAGES") or 10,
                      system_daily=os.environ.get("SYSTEM_DAILY_IMAGES") or 5000,
                      unlimited_chat_ids=_id_set("UNLIMITED_CHAT_IDS"))


def _is_image_job(update):
    """Photos and files cost allowance; commands and /source uploads don't."""
    message = update.get("message", {})
    if (message.get("caption") or "").strip().startswith("/source"):
        return False
    return "photo" in message or "document" in message


def _chat_id(update):
    return update.get("message", {}).get("chat", {}).get("id")


def handler(event, context):
    # A Function URL event carries the request in "body"/"headers"; callers on
    # the internet cannot set top-level keys, so only an async self-invoke
    # (which also has to carry the secret) can start a worker run.
    if TASK_KEY in event:
        return run_task(event)
    return receive(event, context)


# --- Receiver: the HTTP side Telegram talks to ---

def receive(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not _secret_matches(headers.get("x-telegram-bot-api-secret-token")):
        return _response(403, "forbidden")

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        update = json.loads(body)
    except ValueError:
        # Answer 200 so Telegram doesn't keep re-sending something we can't parse
        print("Ignoring update with an unparseable body")
        return _response(200)

    chat_id = _chat_id(update)
    if chat_id is None:
        return _response(200)

    allowed = _allowed_chat_ids()
    if allowed:
        # Kept as a hard allowlist for one release so an existing lockdown doesn't silently open up
        print("ALLOWED_CHAT_IDS is deprecated: the bot now uses daily quotas. Set UNLIMITED_CHAT_IDS "
              "(deploy.sh --unlimited-chat-ids) and clear ALLOWED_CHAT_IDS (--allowed-chat-ids '').")
        if str(chat_id) not in allowed:
            print(f"Ignoring message from chat {chat_id}: not in ALLOWED_CHAT_IDS")
            watermarker.tele.send_telegram(os.environ["BOT_TOKEN"], str(chat_id),
                                           f"This bot is private. Your chat id is {chat_id}.")
            return _response(200)

    # Refuse here, before a 2 GB worker starts, when there's no allowance left at all.
    # The worker makes the authoritative reservation once it knows how many images there are.
    if _is_image_job(update):
        quota = _quota()
        if quota is not None:
            try:
                refusal = quota.check(chat_id)
            except Exception as e:
                print(f"Quota check failed for {chat_id}, leaving it to the worker: {e}")
                refusal = None
            if refusal:
                print(f"quota chat={chat_id} refused before processing")
                watermarker.tele.send_telegram(os.environ["BOT_TOKEN"], str(chat_id), refusal)
                return _response(200)

    _client("lambda").invoke(
        FunctionName=context.invoked_function_arn,
        InvocationType="Event",
        Payload=json.dumps({TASK_KEY: update, "secret": os.environ["WEBHOOK_SECRET"]}).encode("utf-8"),
    )
    return _response(200)


# --- Worker: does the actual watermarking ---

def _prepare_work_dir():
    """Run the bot out of a clean writable directory (/tmp is the only one on Lambda)."""
    os.makedirs(WORK_DIR, exist_ok=True)
    os.chdir(WORK_DIR)
    # watermarker.py falls back to "sun.webp" in the working directory
    if not os.path.exists("sun.webp"):
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(watermarker.__file__)), "sun.webp"), "sun.webp")
    # A warm container reuses /tmp, so drop anything a previous run left behind
    for directory in (watermarker.TEMP_DIR, watermarker.SETTINGS_DIR, watermarker.WATERMARKS_DIR):
        shutil.rmtree(directory, ignore_errors=True)
    watermarker.ensure_dirs()


def _state_files(chat_id):
    """(S3 key, local path) for each piece of per-chat configuration."""
    return [
        (f"settings/{chat_id}.json", watermarker.get_settings_path(chat_id)),
        (f"watermarks/{chat_id}.png", watermarker.get_watermark_path(chat_id)),
    ]


def _digest(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def pull_state(bucket, chat_id):
    """Copy the chat's configuration from S3 to local disk. Returns digests to diff against later."""
    s3 = _client("s3")
    before = {}
    for key, path in _state_files(chat_id):
        try:
            s3.download_file(bucket, key, path)
        except Exception as e:
            # botocore's ClientError; a missing object just means nothing configured yet
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code not in ("404", "NoSuchKey"):
                raise
            if os.path.exists(path):
                os.remove(path)
        before[key] = _digest(path)
    return before


def push_state(bucket, chat_id, before):
    """Write back only what the command changed (e.g. /size, /source, /start)."""
    s3 = _client("s3")
    for key, path in _state_files(chat_id):
        after = _digest(path)
        if after == before.get(key):
            continue
        if after is None:
            s3.delete_object(Bucket=bucket, Key=key)
        else:
            s3.upload_file(path, bucket, key)


def run_task(event):
    if not _secret_matches(event.get("secret")):
        print("Rejecting task without a valid secret")
        return {"ok": False}

    update = event[TASK_KEY]
    chat_id = _chat_id(update)
    if chat_id is None:
        return {"ok": True}

    bot_token = os.environ["BOT_TOKEN"]
    bucket = os.environ["STATE_BUCKET"]

    _prepare_work_dir()
    try:
        watermarker.QUOTA = _quota()
        before = pull_state(bucket, chat_id)
        watermarker.handle_update(bot_token, update)
        push_state(bucket, chat_id, before)
    except Exception as e:
        # handle_update reports its own errors; this covers S3 trouble
        print(f"Error running task for {chat_id}: {e}")
        watermarker.tele.send_telegram(bot_token, str(chat_id),
                                       f"Error: {watermarker._describe_error(e)}")
    finally:
        shutil.rmtree(watermarker.TEMP_DIR, ignore_errors=True)
    return {"ok": True}
