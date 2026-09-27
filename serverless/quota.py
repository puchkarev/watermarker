"""Image quotas and bought credits for the serverless deployment, kept in DynamoDB.

Every chat gets FREE_MONTHLY_IMAGES images per UTC calendar month, except the
chats in UNLIMITED_CHAT_IDS, and the whole deployment gets SYSTEM_DAILY_IMAGES
free images per UTC day. One photo is one image, a zip of 20 is 20, commands
are free.

Beyond the free allowance a chat spends credits it bought with Telegram Stars
(see payments.py): the free allowance first, then the balance. Bought images do
not count towards the system cap - once someone has paid, a busy day for free
users must not turn them away - so the cap only ever limits free usage. A job
that needs more than free + balance is refused whole, and nothing is taken.

One case needs the user's say-so: a chat with free images left on a day the cap
is reached. Running its job on bought images instead would spend money it
expected not to spend, so it is refused with an explanation until the chat
sends /usecredits, which allows it for the rest of that UTC day.

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
counter below zero. A reservation remembers how many images came from the free
allowance and how many from the balance, and a release gives bought images
back first: credits never expire, the free allowance does.

Items (pk / sk):
    system / usage#2026-09-26         free images that day, all chats together
    chat#<id> / usage#2026-09         the chat's free images that month
    chat#<id> / balance               bought images not yet used
    chat#<id> / charge#<charge id>    one purchase, for listing a chat's history
    charge#<charge id> / charge       the same purchase, found by charge id alone:
                                      its write is what makes crediting idempotent
    chat#<id> / credits-when-capped#<day>
                                      the chat's /usecredits consent for that day
    chat#<id> / images#2026-09        every image the chat was given that month,
                                      free or bought: its usage history
    chat#<id> / grant#<timestamp>     images an admin added (admin.py), with who and why
Usage rows carry expires_at (epoch seconds) so DynamoDB TTL deletes them once
their period and the one after it are over; a consent row, once its day is over.
Balance, charge, images and grant rows have no expires_at at all, so TTL never
touches them. Keying by owner then record type keeps everything about one chat in
one partition, readable with a single Query.
"""
from datetime import datetime, timedelta, timezone


def _utc_now():
    return datetime.now(timezone.utc)


def _next_month(when):
    """Midnight UTC on the first day of the month after when."""
    return (when.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _expires_at(key):
    """TTL for a usage row: the end of the period after its own. None for every other
    record type (balances, charges, totals), which must never expire, so they are
    written without expires_at at all and DynamoDB's TTL never touches them."""
    kind, _, period = key[1].partition("#")
    if kind != "usage":
        return None
    if len(period) == len("2026-09"):
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
        end = _next_month(_next_month(start))
    else:
        start = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=2)
    return int(end.timestamp())


def _attributes(item):
    """A DynamoDB item's attributes as plain values (N as int, S as str), keys dropped."""
    return {k: int(v["N"]) if "N" in v else v.get("S") for k, v in item.items() if k not in ("pk", "sk")}


def _error_code(e):
    """The error code of a botocore ClientError (or anything shaped like one)."""
    return getattr(e, "response", {}).get("Error", {}).get("Code")


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
        # Each chat's last granted reservation: its (day, month), so a release settles those
        # rows (a zip at 23:5x on the 30th of September crosses both boundaries), and how
        # many images came from the free allowance and how many from bought credits.
        self._reserved = {}

    # --- counters ---

    def _keys(self, chat_id, period=None):
        now = self.now()
        day, month = period or (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"))
        return ("system", f"usage#{day}"), (f"chat#{chat_id}", f"usage#{month}")

    @staticmethod
    def _item_key(key):
        pk, sk = key
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    def _add(self, key, count):
        """Atomically add count (may be negative) to a (pk, sk) row and return the new total."""
        expression, values = "ADD images :n", {":n": {"N": str(count)}}
        expires_at = _expires_at(key)
        if expires_at is not None:
            expression += " SET expires_at = if_not_exists(expires_at, :exp)"
            values[":exp"] = {"N": str(expires_at)}
        result = self.db.update_item(
            TableName=self.table,
            Key=self._item_key(key),
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
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
            print(f"quota refund failed key={'/'.join(key)} images={count}: {e}")

    def _get(self, key):
        item = self.db.get_item(TableName=self.table, Key=self._item_key(key)).get("Item")
        return int(item["images"]["N"]) if item else 0

    def _take(self, key, count):
        """Atomically subtract count if at least that much is there. False, changing
        nothing, if not (including when the row doesn't exist)."""
        try:
            self.db.update_item(
                TableName=self.table,
                Key=self._item_key(key),
                UpdateExpression="ADD images :n",
                ConditionExpression="images >= :need",
                ExpressionAttributeValues={":n": {"N": str(-count)}, ":need": {"N": str(count)}},
            )
            return True
        except Exception as e:
            if _error_code(e) == "ConditionalCheckFailedException":
                return False
            raise

    def is_unlimited(self, chat_id):
        return str(chat_id) in self.unlimited

    @staticmethod
    def _balance_key(chat_id):
        return f"chat#{chat_id}", "balance"

    def balance(self, chat_id):
        return self._get(self._balance_key(chat_id))

    def status(self, chat_id):
        """Numbers for /balance."""
        _, chat_key = self._keys(chat_id)
        used = self._get(chat_key)
        return {
            "unlimited": self.is_unlimited(chat_id),
            "free_monthly": self.free_monthly,
            "free_left": max(0, self.free_monthly - used),
            "balance": self.balance(chat_id),
            "resets": self.month_resets(),
        }

    # --- purchases ---

    def credit(self, chat_id, user_id, charge_id, pack, images, stars):
        """Add a purchase's images to the chat's balance, exactly once per charge id.

        Telegram re-delivers updates, so the same successful_payment can arrive
        twice. The charge row, the chat's pointer to it and the balance increment
        are one transaction conditional on the charge row not existing yet: either
        all three happen or none do. Returns False for a charge already credited.
        user_id is the payer, which refundStarPayment needs and a group chat id is not.
        """
        when = self.now().isoformat()
        record = {
            "chat_id": {"S": str(chat_id)}, "user_id": {"S": str(user_id)}, "charge_id": {"S": str(charge_id)},
            "pack": {"S": str(pack)}, "images": {"N": str(images)}, "stars": {"N": str(stars)},
            "ts": {"S": when},
        }
        try:
            self.db.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table,
                         "Item": {"pk": {"S": f"charge#{charge_id}"}, "sk": {"S": "charge"}, **record},
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.table,
                         "Item": {"pk": {"S": f"chat#{chat_id}"}, "sk": {"S": f"charge#{charge_id}"}, **record}}},
                # No expires_at: bought credits never expire
                {"Update": {"TableName": self.table, "Key": self._item_key(self._balance_key(chat_id)),
                            "UpdateExpression": "ADD images :n",
                            "ExpressionAttributeValues": {":n": {"N": str(images)}}}},
            ])
        except Exception as e:
            reasons = getattr(e, "response", {}).get("CancellationReasons") or []
            if (_error_code(e) == "TransactionCanceledException" and reasons
                    and reasons[0].get("Code") == "ConditionalCheckFailed"):
                print(f"payment already credited charge={charge_id} chat={chat_id}")
                return False
            raise
        print(f"payment credited chat={chat_id} user={user_id} charge={charge_id} pack={pack} "
              f"images={images} stars={stars}")
        return True

    # --- messages (numbers come from the configured limits) ---

    def _day_resets(self):
        now = self.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return f"00:00 UTC (in {_duration(midnight - now)})"

    def month_resets(self):
        now = self.now()
        start = _next_month(now)
        return f"00:00 UTC on {start.day} {start.strftime('%B')} (in {_duration(start - now)})"

    def _user_message(self, free_left, balance, count):
        available = free_left + balance
        if available >= count:
            # Only reachable if the balance grew after the reservation failed
            return "Your balance changed while this zip was being checked. Please send it again."
        if available == 0:
            if self.free_monthly == 0:
                return "This bot has no free image allowance for your chat. Buy images with /buy."
            return (f"Monthly limit reached: you've used {self.free_monthly} of your {self.free_monthly} free "
                    f"images this month. Buy more with /buy, or wait until your allowance resets at "
                    f"{self.month_resets()}.")
        return (f"That zip contains {count} images, but you have {available} left ({free_left} free this month "
                f"+ {balance} bought), so it is {count - available} short. Nothing was processed - buy more "
                f"with /buy, or split the zip. Your free allowance resets at {self.month_resets()}.")

    def _capped_message(self, free_left, balance, count):
        if self.system_daily == 0:
            why = "This bot isn't processing free images at the moment (its daily limit is 0)"
        else:
            why = (f"The bot's free images for today are used up (its daily limit is {self.system_daily} "
                   f"across all users), so your {free_left} free images this month can't be used until "
                   f"{self._day_resets()}")
        if balance >= count:
            return (f"{why}. You have {balance} bought images: send /usecredits to use them for this zip "
                    "and any others today, then send it again.")
        if balance:
            return (f"{why}. Your {balance} bought images aren't enough for this zip of {count} images "
                    f"({count - balance} short) - buy more with /buy, or try again then.")
        return f"{why}. Bought images aren't affected by this limit - see /buy."

    def _system_message(self, used_before, count):
        """For unlimited chats, which have no balance to fall back on."""
        left = max(0, self.system_daily - used_before)
        if self.system_daily == 0:
            return "This bot isn't processing free images at the moment (its daily limit is 0)."
        if count > self.system_daily:
            return (f"That zip contains {count} images, which is more than the bot's daily limit of "
                    f"{self.system_daily} free images across all users. Try splitting it into smaller zips.")
        if left == 0:
            return (f"The bot has reached its daily limit of {self.system_daily} free images across all users. "
                    f"Please try again after {self._day_resets()}.")
        return (f"That zip contains {count} images, but the bot has {left} of its daily {self.system_daily} "
                f"free images left across all users. Nothing was processed - please try again after "
                f"{self._day_resets()}.")

    # --- the interface watermarker.QUOTA uses ---

    def reserve(self, chat_id, count):
        """Claim count images: free allowance first, then bought credits.
        Returns None if granted, else the refusal message for the user."""
        now = self.now()
        period = (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"))
        system_key, chat_key = self._keys(chat_id, period)

        if self.is_unlimited(chat_id):
            # No personal allowance and no balance, but still within the system cap
            system_total = self._add(system_key, count)
            if system_total > self.system_daily:
                self._refund(system_key, count)
                self._log(chat_id, count, "system", system_total - count, None, 0)
                return self._system_message(system_total - count, count)
            self._granted(chat_id, period, count, 0)
            self._log(chat_id, count, None, system_total, None, 0)
            return None

        # Everything this reservation has taken so far, handed back if a later step
        # fails: an error must not strand a month of free allowance or bought credits
        taken = {"chat": 0, "system": 0, "paid": 0}
        try:
            return self._reserve_limited(chat_id, count, period, taken)
        except Exception:
            self._undo(chat_id, period, taken)
            raise

    def _reserve_limited(self, chat_id, count, period, taken):
        system_key, chat_key = self._keys(chat_id, period)

        # The usage row counts free images only: add the whole job, keep what fits
        chat_total = self._add(chat_key, count)
        taken["chat"] = count
        free_left = max(0, self.free_monthly - (chat_total - count))
        used_before = self.free_monthly - free_left
        free = min(count, free_left)
        if free < count:
            self._refund(chat_key, count - free)
        taken["chat"] = free

        system_total = None
        if free:
            system_total = self._add(system_key, free)
            taken["system"] = free
            if system_total > self.system_daily:
                self._undo(chat_id, period, taken)
                return self._reserve_when_capped(chat_id, count, period, free_left, system_total - free,
                                                 used_before, taken)

        paid = count - free
        if paid:
            if not self._take_balance(chat_id, paid):
                self._undo(chat_id, period, taken)
                self._log(chat_id, count, "chat", None if system_total is None else system_total - free,
                          used_before, 0)
                return self._user_message(free_left, self.balance(chat_id), count)
            taken["paid"] = paid

        self._granted(chat_id, period, free, paid)
        self._log(chat_id, count, None, system_total, used_before + free, paid)
        return None

    def _reserve_when_capped(self, chat_id, count, period, free_left, system_total, used_before, taken):
        """The chat has free images left, but the bot's free images for today are gone.
        Bought images aren't limited by the cap, but using them in place of free ones
        spends the user's money, so that only happens after they said yes (/usecredits)."""
        balance = self.balance(chat_id)
        if balance >= count and self.uses_credits_when_capped(chat_id, period[0]):
            if self._take_balance(chat_id, count):
                taken["paid"] = count
                self._granted(chat_id, period, 0, count)
                self._log(chat_id, count, None, system_total, used_before, count)
                return None
            balance = self.balance(chat_id)
        self._log(chat_id, count, "system", system_total, used_before, 0)
        return self._capped_message(free_left, balance, count)

    def _granted(self, chat_id, period, free, paid):
        self._reserved[str(chat_id)] = {"period": period, "free": free, "paid": paid}
        self._count_images(chat_id, period[1], free + paid)

    def _count_images(self, chat_id, month, count):
        """Usage history for the admin commands. Not part of any limit, so a failure
        here is logged and never fails or refunds the job."""
        try:
            self._add((f"chat#{chat_id}", f"images#{month}"), count)
        except Exception as e:
            print(f"quota history update failed chat={chat_id} images={count}: {e}")

    def _take_balance(self, chat_id, count):
        """Spend count bought images. A failed conditional update is re-checked, because a
        parallel job of the same chat may have returned images since: retry while the
        balance says it would now fit."""
        key = self._balance_key(chat_id)
        for _ in range(3):
            if self._take(key, count):
                return True
            if self.balance(chat_id) < count:
                return False
        return False

    def _undo(self, chat_id, period, taken):
        """Hand back what a reservation in progress has taken (best effort, logged)."""
        system_key, chat_key = self._keys(chat_id, period)
        if taken["chat"]:
            self._refund(chat_key, taken["chat"])
        if taken["system"]:
            self._refund(system_key, taken["system"])
        if taken["paid"]:
            try:
                self._add(self._balance_key(chat_id), taken["paid"])
            except Exception as e:
                print(f"QUOTA BALANCE RETURN FAILED chat={chat_id} images={taken['paid']}: {e}")
        taken.update(chat=0, system=0, paid=0)

    # --- consent to use bought images when the free ones are capped ---

    @staticmethod
    def _consent_key(chat_id, day):
        return f"chat#{chat_id}", f"credits-when-capped#{day}"

    def use_credits_when_capped(self, chat_id):
        """Record, for the rest of today (UTC), that this chat agrees to spend bought images
        while the bot's daily free images are used up. The row deletes itself via TTL."""
        now = self.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        pk, sk = self._consent_key(chat_id, now.strftime("%Y-%m-%d"))
        self.db.put_item(TableName=self.table, Item={
            "pk": {"S": pk}, "sk": {"S": sk}, "expires_at": {"N": str(int(tomorrow.timestamp()))}})
        return self._day_resets()

    def uses_credits_when_capped(self, chat_id, day):
        key = self._item_key(self._consent_key(chat_id, day))
        return "Item" in self.db.get_item(TableName=self.table, Key=key)

    def release(self, chat_id, count):
        """Hand back images that were reserved but not delivered: bought ones first."""
        if count <= 0:
            return
        reservation = self._reserved.get(str(chat_id)) or {"period": None, "free": count, "paid": 0}
        paid = min(count, reservation["paid"])
        free = min(count - paid, reservation["free"])
        reservation["paid"] -= paid
        reservation["free"] -= free

        if paid:
            try:
                self._add(self._balance_key(chat_id), paid)
            except Exception as e:
                # Bought credits are money: make a failed return impossible to miss in the logs
                print(f"QUOTA BALANCE RETURN FAILED chat={chat_id} images={paid}: {e}")
        if free:
            system_key, chat_key = self._keys(chat_id, reservation["period"])
            self._refund(system_key, free)
            if not self.is_unlimited(chat_id):
                self._refund(chat_key, free)
        month = (reservation["period"] or (None, self.now().strftime("%Y-%m")))[1]
        self._count_images(chat_id, month, -(free + paid))
        print(f"quota release chat={chat_id} images={count} free={free} paid={paid}")

    def check(self, chat_id):
        """Cheap read for the receiver: a refusal message if nothing at all is left, else None."""
        system_key, chat_key = self._keys(chat_id)
        system_exhausted = self._get(system_key) >= self.system_daily
        if self.is_unlimited(chat_id):
            return self._system_message(self.system_daily, 1) if system_exhausted else None
        free_left = max(0, self.free_monthly - self._get(chat_key))
        if free_left and not system_exhausted:
            return None
        balance = self.balance(chat_id)
        if balance:
            return None
        if free_left:
            return self._capped_message(free_left, 0, 1)
        return self._user_message(0, 0, 1)

    # --- reads and changes for the admin commands (admin.py) ---

    def chat_records(self, chat_id):
        """Every row of one chat, {sk: attributes}: a single Query thanks to the sort key."""
        records, start = {}, None
        while True:
            kwargs = {"TableName": self.table, "KeyConditionExpression": "pk = :pk",
                      "ExpressionAttributeValues": {":pk": {"S": f"chat#{chat_id}"}}}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.db.query(**kwargs)
            for item in page.get("Items", []):
                records[item["sk"]["S"]] = _attributes(item)
            start = page.get("LastEvaluatedKey")
            if not start:
                return records

    def all_records(self):
        """Every row in the table as (pk, sk, attributes). A Scan: fine at this bot's
        size, and only ever run by an admin command."""
        start = None
        while True:
            kwargs = {"TableName": self.table}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.db.scan(**kwargs)
            for item in page.get("Items", []):
                yield item["pk"]["S"], item["sk"]["S"], _attributes(item)
            start = page.get("LastEvaluatedKey")
            if not start:
                return

    def system_used_today(self):
        return self._get(self._keys(0)[0])

    def charge(self, charge_id):
        item = self.db.get_item(TableName=self.table,
                                Key=self._item_key((f"charge#{charge_id}", "charge"))).get("Item")
        return _attributes(item) if item else None

    def grant(self, chat_id, images, admin, reason):
        """Add images to a chat's balance without a payment, recorded like a purchase so
        the chat's history shows where its balance came from. Returns the new balance."""
        when = self.now().isoformat()
        self.db.put_item(TableName=self.table, Item={
            "pk": {"S": f"chat#{chat_id}"}, "sk": {"S": f"grant#{when}"}, "images": {"N": str(images)},
            "admin": {"S": str(admin)}, "reason": {"S": reason}, "ts": {"S": when}})
        return self._add(self._balance_key(chat_id), images)

    # A refund goes through two recorded states, because the Telegram call in between
    # can fail in a way that says nothing about whether the Stars moved:
    #   begin_refund   refund_pending is set before Telegram is called
    #   finish_refund  refunded is set (once, conditionally) after Telegram confirms
    #   abandon_refund refund_pending is cleared after Telegram definitively refuses
    # A charge left pending is finished by simply trying again: Telegram never pays a
    # charge twice (it answers CHARGE_ALREADY_REFUNDED), and only one finish_refund can
    # win, so the images are taken back exactly once.

    def begin_refund(self, charge_id, chat_id):
        """Mark a refund as in progress. False if the charge doesn't exist or is already
        refunded; an earlier pending attempt is resumed."""
        if not self._update_charge(charge_id, "SET refund_pending = :when",
                                   "attribute_exists(pk) AND attribute_not_exists(refunded)"):
            return False
        self._mirror(charge_id, chat_id, "SET refund_pending = :when")
        return True

    def finish_refund(self, charge_id, chat_id):
        """Record the refund as done. True only for the one call that records it."""
        if not self._update_charge(charge_id, "SET refunded = :when REMOVE refund_pending",
                                   "attribute_exists(pk) AND attribute_not_exists(refunded)"):
            return False
        self._mirror(charge_id, chat_id, "SET refunded = :when REMOVE refund_pending")
        return True

    def abandon_refund(self, charge_id, chat_id):
        """Clear a pending refund that Telegram refused. Never raises: if it fails the
        charge stays pending, which the next attempt resolves, and the log says which."""
        try:
            self.db.update_item(TableName=self.table, Key=self._item_key((f"charge#{charge_id}", "charge")),
                                UpdateExpression="REMOVE refund_pending")
        except Exception as e:
            print(f"REFUND ROLLBACK FAILED charge={charge_id} (it stays pending; /refund resolves it): {e}")
            return
        self._mirror(charge_id, chat_id, "REMOVE refund_pending")

    def _mirror(self, charge_id, chat_id, expression):
        # The chat's copy is for display (/charges); the charge row is the source of truth
        kwargs = {"TableName": self.table, "Key": self._item_key((f"chat#{chat_id}", f"charge#{charge_id}")),
                  "UpdateExpression": expression}
        if ":when" in expression:
            kwargs["ExpressionAttributeValues"] = {":when": {"S": self.now().isoformat()}}
        try:
            self.db.update_item(**kwargs)
        except Exception as e:
            print(f"refund state on the chat's copy failed chat={chat_id} charge={charge_id}: {e}")

    def _update_charge(self, charge_id, expression, condition):
        try:
            self.db.update_item(
                TableName=self.table, Key=self._item_key((f"charge#{charge_id}", "charge")),
                UpdateExpression=expression, ConditionExpression=condition,
                ExpressionAttributeValues={":when": {"S": self.now().isoformat()}})
            return True
        except Exception as e:
            if _error_code(e) == "ConditionalCheckFailedException":
                return False
            raise

    def deduct(self, chat_id, images):
        """Take up to images from the balance, never below zero.
        Returns (images actually taken, balance after)."""
        key = self._balance_key(chat_id)
        total = self._add(key, -images)
        if total >= 0:
            return images, total
        # Part of the pack was already spent: stop at zero rather than owing images
        self._add(key, -total)
        return images + total, 0

    def _log(self, chat_id, count, blocked, system_total, chat_total, paid):
        # One line per request: per-chat usage accounting for the logs
        chat = "unlimited" if chat_total is None else f"{chat_total}/{self.free_monthly}"
        system = "-" if system_total is None else f"{system_total}/{self.system_daily}"
        print(f"quota chat={chat_id} images={count} chat_total={chat} paid={paid} "
              f"system_total={system} blocked={blocked or 'none'}")
