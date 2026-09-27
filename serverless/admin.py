"""Admin commands for the owner's chats (serverless deployment only).

Only chats listed in ADMIN_CHAT_IDS can use these. That list is separate from
UNLIMITED_CHAT_IDS on purpose: "has no quota" and "can read everyone's usage and
move money" are different privileges. Use private chats: in a group, every member
of an admin chat would be an admin.

For anyone else these commands behave exactly like any unknown command: the bot
stays silent and nothing is logged that names them. The Function URL is public,
and a "not authorised" reply would confirm the commands exist.

The two commands that change anything, /grant and /refund, write an "ADMIN AUDIT"
line to the logs with who, what, which chat or charge, and the before and after.
They are the only commands in the system that move credits or money.
"""
import payments

PAGE_SIZE = 20
RECENT_CHARGES = 5
MAX_GRANT = 100000

HELP_TEXT = (
    "Admin commands\n"
    "/users [page] - Chats with any usage or balance, most active this month first.\n"
    "/usage <chat id> - One chat: this month, balance, lifetime total, recent purchases.\n"
    "/charges <chat id> - A chat's purchases, with charge ids for /refund.\n"
    "/limits - The configured limits and today's free usage.\n"
    "/stats - Totals across all chats.\n"
    "/grant <chat id> <images> [reason] - Add images to a chat's balance. No money moves.\n"
    "/refund <charge id> - Show what refunding that purchase would do.\n"
    "/refund <charge id> confirm - Refund the Stars and take the pack's images back."
)


def handle_command(bot_token, chat_id, text, quota, admins):
    """True if text was an admin command from an admin chat (and has been answered)."""
    if str(chat_id) not in admins:
        return False
    words = text.split()
    command = words[0].split("@")[0].lower() if words else ""
    handler = COMMANDS.get(command)
    if handler is None:
        return False
    if quota is None:
        reply = "Image limits aren't enabled on this bot, so there is nothing to administer."
    else:
        try:
            reply = handler(bot_token, chat_id, words[1:], quota)
        except Exception as e:
            print(f"admin command {command} failed for {chat_id}: {e}")
            reply = f"Error: {e}"
    payments._send(bot_token, chat_id, reply)
    return True


def _audit(admin, action, **fields):
    details = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in fields.items())
    print(f"ADMIN AUDIT admin={admin} action={action} {details}")


def _chat_arg(args, usage):
    if not args:
        raise _Usage(usage)
    try:
        return int(args[0])
    except ValueError:
        raise _Usage(usage)


class _Usage(Exception):
    pass


def _usage_reply(handler):
    """Turn a missing or malformed argument into the command's usage line."""
    def wrapper(bot_token, chat_id, args, quota):
        try:
            return handler(bot_token, chat_id, args, quota)
        except _Usage as e:
            return f"Usage: {e}"
    return wrapper


# --- read-only ---

def _admin(bot_token, chat_id, args, quota):
    return HELP_TEXT


def _month(quota):
    return quota.now().strftime("%Y-%m")


def _chats(quota):
    """{chat id: {"month", "free", "balance", "lifetime"}} for every chat with a record."""
    month, chats = _month(quota), {}
    for pk, sk, attrs in quota.all_records():
        if not pk.startswith("chat#"):
            continue
        row = chats.setdefault(pk[len("chat#"):], {"month": 0, "free": 0, "balance": 0, "lifetime": 0})
        images = attrs.get("images", 0)
        if sk == f"images#{month}":
            row["month"] += images
        if sk.startswith("images#"):
            row["lifetime"] += images
        elif sk == f"usage#{month}":
            row["free"] = images
        elif sk == "balance":
            row["balance"] = images
    return {chat: row for chat, row in chats.items() if row["month"] or row["balance"] or row["lifetime"]}


def _users(bot_token, chat_id, args, quota):
    page = int(args[0]) if args and args[0].isdigit() and int(args[0]) > 0 else 1
    chats = sorted(_chats(quota).items(), key=lambda kv: (-kv[1]["month"], -kv[1]["lifetime"], kv[0]))
    if not chats:
        return "No chats have used the bot yet."
    pages = (len(chats) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    lines = [f"Chats {len(chats)} - page {page} of {pages} (this month / free / bought left / lifetime)"]
    for chat, row in chats[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
        marker = " (unlimited)" if quota.is_unlimited(chat) else ""
        lines.append(f"{chat}{marker}: {row['month']} / {row['free']} / {row['balance']} / {row['lifetime']}")
    if page < pages:
        lines.append(f"Next: /users {page + 1}")
    return "\n".join(lines)


def _charge_line(charge_id, attrs):
    when = (attrs.get("ts") or "")[:10]
    refunded = f" - REFUNDED {attrs['refunded'][:10]}" if attrs.get("refunded") else ""
    return (f"{when} {attrs.get('pack')}: {attrs.get('images')} images for {attrs.get('stars')} Stars, "
            f"payer {attrs.get('user_id')}, charge {charge_id}{refunded}")


def _charges_of(records):
    charges = [(sk[len("charge#"):], attrs) for sk, attrs in records.items() if sk.startswith("charge#")]
    return sorted(charges, key=lambda c: c[1].get("ts") or "", reverse=True)


@_usage_reply
def _usage(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/usage <chat id>")
    records = quota.chat_records(target)
    month = _month(quota)
    this_month = records.get(f"images#{month}", {}).get("images", 0)
    free = records.get(f"usage#{month}", {}).get("images", 0)
    lifetime = sum(a.get("images", 0) for sk, a in records.items() if sk.startswith("images#"))
    lines = [f"Chat {target}" + (" (unlimited)" if quota.is_unlimited(target) else ""),
             f"This month: {this_month} images, {free} of {quota.free_monthly} free",
             f"Bought images left: {records.get('balance', {}).get('images', 0)}",
             f"Lifetime: {lifetime} images"]
    charges = _charges_of(records)
    if charges:
        lines.append(f"Purchases ({len(charges)}, latest first):")
        lines += ["- " + _charge_line(cid, a) for cid, a in charges[:RECENT_CHARGES]]
        if len(charges) > RECENT_CHARGES:
            lines.append(f"All of them: /charges {target}")
    return "\n".join(lines)


@_usage_reply
def _charges(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/charges <chat id>")
    charges = _charges_of(quota.chat_records(target))
    if not charges:
        return f"Chat {target} hasn't bought anything."
    return "\n".join([f"Purchases of chat {target}, latest first:"] +
                     ["- " + _charge_line(cid, a) for cid, a in charges])


def _limits(bot_token, chat_id, args, quota):
    used = quota.system_used_today()
    packs = ", ".join(f"{name} {p['images']} images for {p['stars']} Stars" for name, p in payments.PACKS.items())
    return (f"Free images per chat: {quota.free_monthly} a month\n"
            f"Free images for the whole bot: {quota.system_daily} a day - {used} used today, "
            f"{max(0, quota.system_daily - used)} left\n"
            f"Unlimited chats: {', '.join(sorted(quota.unlimited)) or 'none'}\n"
            f"Packs: {packs}")


def _stats(bot_token, chat_id, args, quota):
    month = _month(quota)
    images_month = active = balances = sold = stars = refunds = 0
    for pk, sk, attrs in quota.all_records():
        images = attrs.get("images", 0)
        if pk.startswith("chat#") and sk == f"images#{month}" and images:
            images_month += images
            active += 1
        elif pk.startswith("chat#") and sk == "balance":
            balances += images
        elif pk.startswith("charge#"):
            if attrs.get("refunded"):
                refunds += 1
            else:
                sold += images
                stars += attrs.get("stars", 0)
    return (f"This month: {images_month} images for {active} chats\n"
            f"Free images today: {quota.system_used_today()} of {quota.system_daily}\n"
            f"Sold: {sold} images for {stars} Stars ({refunds} refunded purchases not counted)\n"
            f"Bought images not yet used: {balances}")


# --- changes: audited ---

@_usage_reply
def _grant(bot_token, chat_id, args, quota):
    usage = "/grant <chat id> <images> [reason]"
    target = _chat_arg(args, usage)
    if len(args) < 2 or not args[1].isdigit() or not 0 < int(args[1]) <= MAX_GRANT:
        raise _Usage(f"{usage} - images must be 1 to {MAX_GRANT}")
    images, reason = int(args[1]), " ".join(args[2:])
    after = quota.grant(target, images)
    _audit(chat_id, "grant", chat=target, images=images, reason=reason,
           balance_before=after - images, balance_after=after)
    return f"Granted {images} images to chat {target}. Its balance went from {after - images} to {after}."


@_usage_reply
def _refund(bot_token, chat_id, args, quota):
    if not args:
        raise _Usage("/refund <charge id> [confirm]")
    charge_id, confirm = args[0], args[1:] == ["confirm"]
    charge = quota.charge(charge_id)
    if charge is None:
        return f"No purchase has charge id {charge_id}."
    if charge.get("refunded"):
        return f"Charge {charge_id} was already refunded on {charge['refunded'][:10]}."
    target, images, stars, payer = charge["chat_id"], charge["images"], charge["stars"], charge["user_id"]
    balance = quota.balance(target)
    shortfall = max(0, images - balance)
    note = (f" Only {balance} of its {images} images are left: the balance will go to 0, and the other "
            f"{shortfall} were already used." if shortfall else "")

    if not confirm:
        return (f"Charge {charge_id}: {_charge_line(charge_id, charge)}\n"
                f"Refunding sends {stars} Stars back to user {payer} and takes {images} images from chat "
                f"{target} (balance {balance} -> {max(0, balance - images)}).{note}\n"
                f"To go ahead: /refund {charge_id} confirm")

    # Flag first, so a repeated confirm can never reach Telegram twice
    if not quota.mark_refunded(charge_id, target):
        return f"Charge {charge_id} was already refunded."
    try:
        payments.refund_star_payment(bot_token, payer, charge_id)
    except Exception as e:
        quota.unmark_refunded(charge_id, target)
        _audit(chat_id, "refund-failed", charge=charge_id, chat=target, user=payer, error=str(e))
        return f"Telegram refused the refund, so nothing changed: {e}"
    # The Stars have gone back: from here on, whatever happens must be on record
    try:
        taken, after = quota.deduct(target, images)
    except Exception as e:
        _audit(chat_id, "refund", charge=charge_id, chat=target, user=payer, stars=stars, images=images,
               images_taken=0, balance_before=balance, error=str(e))
        return (f"Refunded {stars} Stars to user {payer} for charge {charge_id}, but taking the {images} "
                f"images back from chat {target} failed: {e}. The chat still has them; check with "
                f"/usage {target}.")
    _audit(chat_id, "refund", charge=charge_id, chat=target, user=payer, stars=stars, images=images,
           images_taken=taken, balance_before=balance, balance_after=after)
    reply = (f"Refunded {stars} Stars to user {payer} for charge {charge_id}. Took {taken} images from chat "
             f"{target}; its balance is now {after}.")
    if taken < images:
        reply += f" {images - taken} of the pack's images had already been used and could not be taken back."
    return reply


COMMANDS = {
    "/admin": _admin,
    "/users": _users,
    "/usage": _usage,
    "/charges": _charges,
    "/limits": _limits,
    "/stats": _stats,
    "/grant": _grant,
    "/refund": _refund,
}
