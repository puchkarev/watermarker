"""Image quotas for the serverless deployment, kept in DynamoDB.

Every chat gets FREE_MONTHLY_IMAGES images per UTC calendar month, except the
chats in UNLIMITED_CHAT_IDS, and the whole deployment gets SYSTEM_DAILY_IMAGES
per UTC day. One photo is one image, a zip of 20 is 20, commands are free.

Counters are DynamoDB items updated with an atomic ADD, because many workers run
at once (every zip in a batch is its own invocation) and a read-modify-write
would lose increments exactly under the load the quota exists to control.

Reserve, then settle: a job adds its image count up front and, if that takes a
counter past its limit, subtracts it again and is refused. Checking first and
adding afterwards would let parallel jobs overshoot. Images that fail or never
reach the user are subtracted afterwards (release), so only successes count.

A known, self-healing tradeoff: a refused job briefly inflates a counter before
its refund lands, so a small job racing with it can be refused even though it
would have fit. Retrying moments later succeeds. That is the price of never
overshooting the cap, not a bug.

Releases go back to the month and day the images were reserved in, even when a
job runs past midnight or into the next month, and a refund never takes a
counter below zero.

Items:  pk = "system#2026-09-26" (per day) or "chat#<id>#2026-09" (per month),
        images = count, expires_at = epoch seconds for DynamoDB TTL to delete
        the row once its period and the one after it are over.
"""
from datetime import datetime, timedelta, timezone


def _utc_now():
    return datetime.now(timezone.utc)


def _next_month(when):
    """Midnight UTC on the first day of the month after when."""
    return (when.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _expires_at(key):
    """Keep a row until the end of the period after its own, then let TTL delete it."""
    period = key.rsplit("#", 1)[-1]
    if len(period) == len("2026-09"):
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
        end = _next_month(_next_month(start))
    else:
        start = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=2)
    return int(end.timestamp())


def _duration(delta):
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}d {minutes // 60 % 24}h"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class ImageQuota:
    def __init__(self, dynamodb, table, free_monthly, system_daily, unlimited_chat_ids=(), now=_utc_now):
        self.db = dynamodb
        self.table = table
        self.free_monthly = int(free_monthly)
        self.system_daily = int(system_daily)
        self.unlimited = {str(c) for c in unlimited_chat_ids}
        self.now = now
        # (day, month) of each chat's last granted reservation, so its release settles those rows.
        # The two boundaries are independent: a zip at 23:5x on the 30th of September crosses both.
        self._reserved_period = {}

    # --- counters ---

    def _keys(self, chat_id, period=None):
        now = self.now()
        day, month = period or (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"))
        return f"system#{day}", f"chat#{chat_id}#{month}"

    def _add(self, key, count):
        """Atomically add count (may be negative) and return the new total."""
        result = self.db.update_item(
            TableName=self.table,
            Key={"pk": {"S": key}},
            UpdateExpression="ADD images :n SET expires_at = if_not_exists(expires_at, :exp)",
            ExpressionAttributeValues={":n": {"N": str(count)}, ":exp": {"N": str(_expires_at(key))}},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"]["images"]["N"])

    def _refund(self, key, count):
        """Subtract count, never below zero. Errors are logged, not raised: a failed
        refund only leaves allowance unused until the row's period ends."""
        try:
            total = self._add(key, -count)
            if total < 0:
                self._add(key, -total)
        except Exception as e:
            print(f"quota refund failed key={key} images={count}: {e}")

    def _get(self, key):
        item = self.db.get_item(TableName=self.table, Key={"pk": {"S": key}}).get("Item")
        return int(item["images"]["N"]) if item else 0

    def is_unlimited(self, chat_id):
        return str(chat_id) in self.unlimited

    # --- messages (numbers come from the configured limits) ---

    def _day_resets(self):
        now = self.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return f"00:00 UTC (in {_duration(midnight - now)})"

    def _month_resets(self):
        now = self.now()
        start = _next_month(now)
        return f"00:00 UTC on {start.day} {start.strftime('%B')} (in {_duration(start - now)})"

    def _user_message(self, used_before, count):
        left = max(0, self.free_monthly - used_before)
        if self.free_monthly == 0:
            return "This bot has no free image allowance for your chat."
        if count > self.free_monthly:
            # Would be refused every month, so say that rather than this month's remainder
            return (f"That zip contains {count} images, which is more than your monthly allowance of "
                    f"{self.free_monthly}. Try splitting it into smaller zips.")
        if left == 0:
            return (f"Monthly limit reached: you've used {self.free_monthly} of your {self.free_monthly} images "
                    f"this month. Your allowance resets at {self._month_resets()}.")
        return (f"That zip contains {count} images, but you have {left} of your {self.free_monthly} left this "
                f"month. Nothing was processed - your allowance resets at {self._month_resets()}.")

    def _system_message(self, used_before, count):
        left = max(0, self.system_daily - used_before)
        if self.system_daily == 0:
            return "This bot isn't processing images at the moment (its daily limit is 0)."
        if count > self.system_daily:
            return (f"That zip contains {count} images, which is more than the bot's daily limit of "
                    f"{self.system_daily} across all users. Try splitting it into smaller zips.")
        if left == 0:
            return (f"The bot has reached its daily limit of {self.system_daily} images across all users. "
                    f"Please try again after {self._day_resets()}.")
        return (f"That zip contains {count} images, but the bot has {left} of its daily {self.system_daily} "
                f"left across all users. Nothing was processed - please try again after {self._day_resets()}.")

    # --- the interface watermarker.QUOTA uses ---

    def reserve(self, chat_id, count):
        """Claim count images. Returns None if granted, else the refusal message for the user."""
        now = self.now()
        period = (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"))
        system_key, chat_key = self._keys(chat_id, period)

        system_total = self._add(system_key, count)
        if system_total > self.system_daily:
            self._refund(system_key, count)
            self._log(chat_id, count, "system", system_total - count, None)
            return self._system_message(system_total - count, count)

        chat_total = None
        if not self.is_unlimited(chat_id):
            chat_total = self._add(chat_key, count)
            if chat_total > self.free_monthly:
                self._refund(chat_key, count)
                self._refund(system_key, count)
                self._log(chat_id, count, "chat", system_total - count, chat_total - count)
                return self._user_message(chat_total - count, count)

        self._reserved_period[str(chat_id)] = period
        self._log(chat_id, count, None, system_total, chat_total)
        return None

    def release(self, chat_id, count):
        """Hand back images that were reserved but not delivered."""
        if count <= 0:
            return
        system_key, chat_key = self._keys(chat_id, self._reserved_period.get(str(chat_id)))
        self._refund(system_key, count)
        if not self.is_unlimited(chat_id):
            self._refund(chat_key, count)
        print(f"quota release chat={chat_id} images={count}")

    def check(self, chat_id):
        """Cheap read for the receiver: a refusal message if nothing at all is left, else None."""
        system_key, chat_key = self._keys(chat_id)
        system_used = self._get(system_key)
        if system_used >= self.system_daily:
            return self._system_message(system_used, 1)
        if not self.is_unlimited(chat_id):
            chat_used = self._get(chat_key)
            if chat_used >= self.free_monthly:
                return self._user_message(chat_used, 1)
        return None

    def _log(self, chat_id, count, blocked, system_total, chat_total):
        # One line per request: per-chat usage accounting for the logs
        chat = "unlimited" if chat_total is None else f"{chat_total}/{self.free_monthly}"
        print(f"quota chat={chat_id} images={count} chat_total={chat} "
              f"system_total={system_total}/{self.system_daily} blocked={blocked or 'none'}")
