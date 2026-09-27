"""Admin commands for the owner's chats (serverless deployment only).

Only admin chats can use these: the root admins in ADMIN_CHAT_IDS, plus any they
/promote. Admins are separate from unlimited chats on purpose: "has no quota" and
"can read everyone's usage and move money" are different privileges. Admins must be
private chats: in a group, every member would be an admin, so /promote refuses groups.

Root entries (ADMIN_CHAT_IDS, UNLIMITED_CHAT_IDS) can't be removed from chat, by
anyone. That floor lives outside the database, so a mistaken /demote or a hostile
admin can always be put right with a deploy.

For anyone else these commands behave exactly like any unknown command: the bot
stays silent and nothing is logged that names them. The Function URL is public,
and a "not authorised" reply would confirm the commands exist.

Every admin command, read-only or not, writes an "ADMIN COMMAND" line to the logs
with the admin's chat id and the arguments. The ones that change anything also write
an "ADMIN AUDIT" line with what changed and the before and after: /grant and /refund,
the only commands in the system that move credits or money, and /promote, /demote,
/giveunlimited and /takeunlimited, the only ones that change who holds a privilege.
"""
import payments

PAGE_SIZE = 20
RECENT_CHARGES = 5
MAX_GRANT = 100000

HELP_TEXT = (
    "Admin commands\n"
    "/users [page] - Every chat that has used the bot, with its @username or name and "
    "when it was last seen. Most active this month first.\n"
    "/usage <chat id> - One chat: this month, balance, lifetime total, recent purchases.\n"
    "/charges <chat id> [page] - A chat's purchases, with charge ids for /refund.\n"
    "/limits - The configured limits and today's free usage.\n"
    "/stats - Totals across all chats.\n"
    "/grant <chat id> <images> [reason] - Add images to a chat's balance. No money moves.\n"
    "/refund <charge id> - Show what refunding that purchase would do.\n"
    "/refund <charge id> confirm - Refund the Stars and take the pack's images back.\n"
    "/admins - Who can use these commands.\n"
    "/promote <chat id> - Make a private chat an admin.\n"
    "/demote <chat id> [confirm] - Take admin away. Root admins (from the deployment) can't be demoted.\n"
    "/unlimited - Chats with no personal limit.\n"
    "/giveunlimited <chat id> - Stop counting a chat's images against its allowance or balance.\n"
    "/takeunlimited <chat id> [confirm] - Count them again. Root entries can't be changed here."
)


def handle_command(bot_token, chat_id, text, quota, admins):
    """True if text was an admin command from an admin chat (and has been answered).
    admins is the root set; admins promoted from chat are read from the quota table."""
    words = text.split()
    command = words[0].split("@")[0].lower() if words else ""
    handler = COMMANDS.get(command)
    if handler is None:
        return False
    if not (str(chat_id) in admins or (quota is not None and quota.is_admin(chat_id))):
        return False
    # Privileged commands, so every one is on record, read-only or not (never for non-admins)
    print(f"ADMIN COMMAND admin={chat_id} command={command} args={' '.join(words[1:])!r}")
    if quota is None:
        reply = "Image limits aren't enabled on this bot, so there is nothing to administer."
    else:
        try:
            reply = handler(bot_token, chat_id, words[1:], quota)
        except Exception as e:
            print(f"admin command {command} failed for {chat_id}: {e}")
            reply = f"Error: {e}"
    try:
        _send_long(bot_token, chat_id, reply)
    except Exception as e:
        print(f"admin reply to {chat_id} failed: {e}")
    return True


def _send_long(bot_token, chat_id, text, limit=4000):
    """Send text in as many messages as Telegram's 4096-character limit needs,
    splitting between lines."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    for chunk in chunks + [current]:
        payments._send(bot_token, chat_id, chunk)


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


def _display_name(profile):
    """How a chat is shown next to its id: @username, else its name, else the group
    title. Only a display hint - usernames change and get reassigned; the id is the chat."""
    if profile.get("username"):
        return "@" + profile["username"]
    name = " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p)
    return name or profile.get("title") or ""


def _label(chat, profile):
    name = _display_name(profile)
    return f"{chat} ({name})" if name else str(chat)


def _chats(quota):
    """{chat id: {"month", "free", "balance", "lifetime", "profile"}} for every chat
    with a record - including chats that only ever sent commands."""
    month, chats = _month(quota), {}
    for pk, sk, attrs in quota.all_records():
        if not pk.startswith("chat#"):
            continue
        row = chats.setdefault(pk[len("chat#"):],
                               {"month": 0, "free": 0, "balance": 0, "lifetime": 0, "profile": {}})
        if sk == "profile":
            row["profile"] = attrs
            continue
        images = attrs.get("images", 0)
        if sk == f"images#{month}":
            row["month"] += images
        if sk.startswith("images#"):
            row["lifetime"] += images
        elif sk == f"usage#{month}":
            row["free"] = images
        elif sk == "balance":
            row["balance"] = images
    return {chat: row for chat, row in chats.items() if any(row.values())}


def _users(bot_token, chat_id, args, quota):
    page = _page_arg(args, 0)
    # Most images this month first, then lifetime, then most recently seen, then chat id:
    # a total order, since each page is its own Scan and must slice the same list.
    # Stable sorts, applied from the last key to the first.
    chats = sorted(_chats(quota).items(), key=lambda kv: kv[0])
    chats = sorted(chats, key=lambda kv: kv[1]["profile"].get("last_seen") or "", reverse=True)
    chats = sorted(chats, key=lambda kv: (-kv[1]["month"], -kv[1]["lifetime"]))
    if not chats:
        return "No chats have used the bot yet."
    pages = (len(chats) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    lines = [f"Chats {len(chats)} - page {page} of {pages} (this month / free / bought left / lifetime)"]
    for chat, row in chats[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
        marker = " (unlimited)" if quota.is_unlimited(chat) else ""
        seen = row["profile"].get("last_seen")
        lines.append(f"{_label(chat, row['profile'])}{marker}: {row['month']} / {row['free']} / "
                     f"{row['balance']} / {row['lifetime']}" + (f", last seen {seen[:16]}" if seen else ""))
    if page < pages:
        lines.append(f"Next: /users {page + 1}")
    return "\n".join(lines)


def _charge_line(charge_id, attrs):
    when = (attrs.get("ts") or "")[:10]
    if attrs.get("refunded"):
        refunded = f" - REFUNDED {attrs['refunded'][:10]}"
    elif attrs.get("refund_pending"):
        refunded = f" - REFUND PENDING since {attrs['refund_pending'][:16]}"
    else:
        refunded = ""
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
    profile = records.get("profile", {})
    lines = [f"Chat {_label(target, profile)}" + (" (unlimited)" if quota.is_unlimited(target) else "")]
    if profile:
        lines.append(f"Last seen {profile.get('last_seen', '')[:16]}, {profile.get('messages', 0)} messages")
    lines += [f"This month: {this_month} images, {free} of {quota.free_monthly} free",
             f"Bought images left: {records.get('balance', {}).get('images', 0)}",
             f"Lifetime: {lifetime} images"]
    charges = _charges_of(records)
    if charges:
        lines.append(f"Purchases ({len(charges)}, latest first):")
        lines += ["- " + _charge_line(cid, a) for cid, a in charges[:RECENT_CHARGES]]
        if len(charges) > RECENT_CHARGES:
            lines.append(f"All of them: /charges {target}")
    return "\n".join(lines)


def _page_arg(args, index):
    arg = args[index] if len(args) > index else ""
    return int(arg) if arg.isdigit() and int(arg) > 0 else 1


@_usage_reply
def _charges(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/charges <chat id> [page]")
    charges = _charges_of(quota.chat_records(target))
    if not charges:
        return f"Chat {target} hasn't bought anything."
    pages = (len(charges) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(_page_arg(args, 1), pages)
    lines = [f"Purchases of chat {target}, latest first - page {page} of {pages}:"]
    lines += ["- " + _charge_line(cid, a) for cid, a in charges[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    if page < pages:
        lines.append(f"Next: /charges {target} {page + 1}")
    return "\n".join(lines)


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
    images_month = active = seen = balances = sold = stars = refunds = 0
    for pk, sk, attrs in quota.all_records():
        images = attrs.get("images", 0)
        if pk.startswith("chat#") and sk == f"images#{month}" and images:
            images_month += images
            active += 1
        elif pk.startswith("chat#") and sk == "balance":
            balances += images
        elif pk.startswith("chat#") and sk == "profile" and (attrs.get("last_seen") or "").startswith(month):
            seen += 1
        elif pk.startswith("charge#"):
            if attrs.get("refunded"):
                refunds += 1
            else:
                sold += images
                stars += attrs.get("stars", 0)
    return (f"This month: {images_month} images for {active} chats; {seen} chats used the bot\n"
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
    after = quota.grant(target, images, chat_id, reason)
    _audit(chat_id, "grant", chat=target, images=images, reason=reason,
           balance_before=after - images, balance_after=after)
    return f"Granted {images} images to chat {target}. Its balance went from {after - images} to {after}."


def _pack_usage(records, charge_id, charge):
    """How many of this pack's images have already been used, counting a chat's
    credits as used oldest first. Credits are pooled, so this is an attribution, not a
    ledger: (credited, left, used, used_from_this_pack)."""
    credits = [(a.get("ts") or "", sk, a.get("images", 0)) for sk, a in records.items()
               if (sk.startswith("charge#") and not a.get("refunded")) or sk.startswith("grant#")]
    credited = sum(images for _, _, images in credits)
    left = records.get("balance", {}).get("images", 0)
    used = max(0, credited - left)
    this = ((charge.get("ts") or ""), f"charge#{charge_id}")
    before = sum(images for ts, sk, images in credits if (ts, sk) < this)
    return credited, left, used, max(0, min(charge["images"], used - before))


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
    credited, balance, used, pack_used = _pack_usage(quota.chat_records(target), charge_id, charge)

    if not confirm:
        lines = [f"Charge {charge_id}: {_charge_line(charge_id, charge)}",
                 f"Refunding sends {stars} Stars back to user {payer} and takes {images} images from chat "
                 f"{target} (balance {balance} -> {max(0, balance - images)})."]
        if pack_used:
            # What the refund takes beyond this pack's own unused images belongs to other credits
            from_others = max(0, min(images, balance) - (images - pack_used))
            line = (f"Chat {target} has been given {credited} bought or granted images and has {balance} left, "
                    f"so {used} have been used. Counting the oldest first, {pack_used} of this pack's {images} "
                    "images were among them.")
            if from_others:
                line += f" The refund therefore takes {from_others} images that came from other purchases or grants."
            lines.append(line)
        if balance < images:
            lines.append(f"Only {balance} images are left, so the balance will go to 0.")
        if charge.get("refund_pending"):
            lines.append(f"A refund of this charge was started {charge['refund_pending'][:16]} and its outcome "
                         "is unknown. Confirming finishes it safely: Telegram never pays a charge twice.")
        lines.append(f"To go ahead: /refund {charge_id} confirm")
        return "\n".join(lines)

    if not quota.begin_refund(charge_id, target):
        return f"Charge {charge_id} was already refunded."
    try:
        payments.refund_star_payment(bot_token, payer, charge_id)
    except payments.TelegramError as e:
        if "CHARGE_ALREADY_REFUNDED" not in str(e):
            quota.abandon_refund(charge_id, target)
            _audit(chat_id, "refund-failed", charge=charge_id, chat=target, user=payer, error=str(e))
            return f"Telegram refused the refund, so nothing changed: {e}"
        # The Stars went back already (an earlier attempt whose answer was lost): finish it
    except Exception as e:
        # No definite answer: the Stars may have gone back. Leave it pending - refunding
        # twice is the expensive mistake, and a retry resolves it either way.
        _audit(chat_id, "refund-unknown", charge=charge_id, chat=target, user=payer, error=str(e))
        return (f"Telegram didn't give a clear answer ({e}), so the {stars} Stars may or may not have gone "
                f"back. Charge {charge_id} is marked as refund pending. Send /refund {charge_id} confirm "
                "again to finish: if the Stars already went back Telegram says so and only the images are "
                "taken back; if not, they're refunded then.")

    if not quota.finish_refund(charge_id, target):
        return f"Charge {charge_id} was already refunded."
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
           images_taken=taken, balance_before=balance, balance_after=after, pack_images_used=pack_used)
    reply = (f"Refunded {stars} Stars to user {payer} for charge {charge_id}. Took {taken} images from chat "
             f"{target}; its balance is now {after}.")
    if taken < images:
        reply += f" {images - taken} could not be taken back: the chat had no images left."
    return reply


# --- who holds a privilege: audited, and the removals confirmed ---

ROOT_NOTE = "root, from the deployment config"


def _profile(quota, chat):
    return quota.chat_records(chat).get("profile", {})


def _members(quota, kind, title):
    members = quota.members(kind)
    if not members:
        return f"{title}: none."
    lines = [f"{title} ({len(members)}):"]
    for chat in sorted(members):
        row = members[chat]
        how = ROOT_NOTE if row is None else f"added by {row.get('added_by')} on {(row.get('ts') or '')[:10]}"
        lines.append(f"- {_label(chat, _profile(quota, chat))}: {how}")
    return "\n".join(lines)


def _admins(bot_token, chat_id, args, quota):
    return _members(quota, "admin", "Admins")


def _unlimited(bot_token, chat_id, args, quota):
    return _members(quota, "unlimited", "Unlimited chats")


@_usage_reply
def _promote(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/promote <chat id>")
    if target < 0:
        return (f"Chat {target} is a group: every member of it would be an admin. "
                "Admins must be private chats - promote the person's own chat instead.")
    if quota.is_admin(target) or not quota.add_member("admin", target, chat_id):
        return f"Chat {target} is already an admin. Nothing changed."
    _audit(chat_id, "promote", chat=target, admin_before=False, admin_after=True)
    return (f"Chat {_label(target, _profile(quota, target))} is now an admin: it can use every admin "
            f"command, from its next message. Undo with /demote {target}.")


@_usage_reply
def _demote(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/demote <chat id> [confirm]")
    if str(target) in quota.root["admin"]:
        return f"{target} is a root admin from the deployment config and can't be demoted here."
    members = quota.members("admin")
    if str(target) not in members:
        return f"Chat {target} isn't an admin. Nothing changed."
    if args[1:] != ["confirm"]:
        lines = [f"Demoting {_label(target, _profile(quota, target))} takes away every admin command, "
                 "from its next message."]
        if str(target) == str(chat_id):
            lines.append("That's this chat: you will lose admin access yourself.")
        if not [c for c, row in members.items() if row is not None and c != str(target)]:
            lines.append("It's the last admin added from chat: only the root admins from the deployment "
                         "config will be left.")
        lines.append(f"To go ahead: /demote {target} confirm")
        return "\n".join(lines)
    if not quota.remove_member("admin", target):
        return f"Chat {target} isn't an admin. Nothing changed."
    _audit(chat_id, "demote", chat=target, admin_before=True, admin_after=False)
    return f"Chat {target} is no longer an admin."


def _allowance(quota, chat):
    """(free images left this month, bought images left) for one chat."""
    records = quota.chat_records(chat)
    used = records.get(f"usage#{_month(quota)}", {}).get("images", 0)
    return max(0, quota.free_monthly - used), records.get("balance", {}).get("images", 0)


@_usage_reply
def _give_unlimited(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/giveunlimited <chat id>")
    if quota.is_unlimited(target) or not quota.add_member("unlimited", target, chat_id):
        return f"Chat {target} is already unlimited. Nothing changed."
    _audit(chat_id, "give-unlimited", chat=target, unlimited_before=False, unlimited_after=True)
    _, balance = _allowance(quota, target)
    return (f"Chat {_label(target, _profile(quota, target))} is now unlimited: from its next image, nothing "
            f"counts against its monthly allowance or its bought images ({balance}, which it keeps). The "
            f"bot's daily limit still applies. Undo with /takeunlimited {target}.")


@_usage_reply
def _take_unlimited(bot_token, chat_id, args, quota):
    target = _chat_arg(args, "/takeunlimited <chat id> [confirm]")
    if str(target) in quota.root["unlimited"]:
        return f"{target} is unlimited from the deployment config and can't be changed here."
    if not quota.is_unlimited(target):
        return f"Chat {target} isn't unlimited. Nothing changed."
    free_left, balance = _allowance(quota, target)
    if args[1:] != ["confirm"]:
        return (f"Taking unlimited from {_label(target, _profile(quota, target))}: from its next image it uses "
                f"its free images ({free_left} of {quota.free_monthly} left this month), then its bought "
                f"images ({balance}).\nTo go ahead: /takeunlimited {target} confirm")
    if not quota.remove_member("unlimited", target):
        return f"Chat {target} isn't unlimited. Nothing changed."
    _audit(chat_id, "take-unlimited", chat=target, unlimited_before=True, unlimited_after=False,
           free_left=free_left, balance=balance)
    return (f"Chat {target} is no longer unlimited. It has {free_left} free images left this month and "
            f"{balance} bought images.")


COMMANDS = {
    "/admin": _admin,
    "/users": _users,
    "/usage": _usage,
    "/charges": _charges,
    "/limits": _limits,
    "/stats": _stats,
    "/grant": _grant,
    "/refund": _refund,
    "/admins": _admins,
    "/promote": _promote,
    "/demote": _demote,
    "/unlimited": _unlimited,
    "/giveunlimited": _give_unlimited,
    "/takeunlimited": _take_unlimited,
}
