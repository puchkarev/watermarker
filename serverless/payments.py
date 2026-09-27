"""Buying image credits with Telegram Stars (serverless deployment only).

A chat that has used its free monthly images can buy a pack of credits. Stars
only ever appear on the invoice: what is stored is a count of images (quota.py).

The flow, and where each step runs:

    /buy           worker    sends one invoice per pack
    pre_checkout   receiver  Telegram asks "may this payment go ahead?" and cancels
                             it unless answered within 10 seconds, so this is
                             answered directly, never handed to the async worker
    paid           receiver  message.successful_payment: the images are credited,
                             idempotently on telegram_payment_charge_id; a storage
                             error answers 500 so Telegram delivers it again

refund_star_payment() reverses a charge. It is deliberately not reachable from any
user command: all purchases are final (TERMS.md), but it is the only way for the
owner to put right a billing mistake.
"""
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TERMS_URL = "https://github.com/puchkarev/watermarker/blob/main/TERMS.md"
SUPPORT_EMAIL = "victor.puchkarev@gmail.com"

# Stars prices are fixed by the terms; changing them means changing TERMS.md too
PACKS = {
    "small": {"images": 100, "stars": 200},
    "large": {"images": 1000, "stars": 1000},
}
PAYLOAD_PREFIX = "images-v1:"

# Added to the Lambda's command menu by deploy.sh, after watermarker.BOT_COMMANDS
PAYMENT_COMMANDS = {
    "balance": "Show your free images left this month and bought images",
    "buy": "Buy more images with Telegram Stars",
    "usecredits": "Use bought images today if the bot's free images run out",
    "terms": "Terms and conditions",
    "paysupport": "Help with payments and billing",
}

HELP_TEXT = (
    "\n\nImages:\n"
    "/balance - Show your free images left this month and the images you've bought.\n"
    "/buy - Buy more images with Telegram Stars.\n"
    "/usecredits - If the bot's free images for the day run out, use your bought images instead "
    "(for the rest of the day).\n"
    "/terms - Terms and conditions.\n"
    "/paysupport - Help with payments and billing."
)

TERMS_TEXT = (
    "Watermarker - terms in brief\n"
    "- 10 images free every month, then credits: 100 for 200 Stars, or 1000 for 1000 Stars.\n"
    "- Credits are image counts, never expire, and aren't transferable or exchangeable for money.\n"
    "- All purchases are final - no refunds.\n"
    "- You're never charged for a failure: failed images and undelivered results cost nothing, "
    "automatically. A zip that doesn't fit is refused whole.\n"
    "- Your images are never retained - they exist only while being processed, and results "
    "aren't kept either.\n"
    "- Provided as-is, with no guarantee of availability.\n"
    f"Full terms: {TERMS_URL}\n"
    "Payment questions: /paysupport"
)

PAYSUPPORT_TEXT = (
    "Payments, credits and billing\n"
    f"Email: {SUPPORT_EMAIL}\n"
    "Include your chat id (send /balance to see it), which pack you bought, and roughly when. "
    "Please don't post payment details in a "
    "public GitHub issue.\n"
    "All purchases are final, so this isn't a refund line - but billing mistakes, such as being "
    "charged for images that were never delivered, will be put right.\n"
    "Bugs and feature requests: https://github.com/puchkarev/watermarker/issues"
)


class TelegramError(Exception):
    """Telegram answered, and refused: nothing happened."""


class TelegramUnknown(Exception):
    """No definite answer (connection error, timeout, a server error, or a response that
    isn't Telegram's): the call may or may not have taken effect."""


def _call(bot_token, method, payload):
    try:
        response = requests.post(TELEGRAM_API.format(token=bot_token, method=method), json=payload, timeout=10)
        body = response.json()
    except Exception as e:
        raise TelegramUnknown(f"{method}: no answer from Telegram ({e})") from e
    if not body.get("ok"):
        code = body.get("error_code", response.status_code)
        if isinstance(code, int) and code >= 500:
            raise TelegramUnknown(f"{method}: Telegram server error {code} ({body.get('description')})")
        raise TelegramError(f"{method} failed: {body.get('description', code)}")
    return body.get("result")


def _send(bot_token, chat_id, text):
    _call(bot_token, "sendMessage", {"chat_id": chat_id, "text": text})


def payload_for(pack):
    return PAYLOAD_PREFIX + pack


def pack_for_payload(payload):
    """The pack an invoice payload names, or None."""
    if not isinstance(payload, str) or not payload.startswith(PAYLOAD_PREFIX):
        return None
    pack = payload[len(PAYLOAD_PREFIX):]
    return pack if pack in PACKS else None


# --- Telegram API ---

def send_invoice(bot_token, chat_id, pack):
    images, stars = PACKS[pack]["images"], PACKS[pack]["stars"]
    _call(bot_token, "sendInvoice", {
        "chat_id": chat_id,
        "title": f"{images} images",
        "description": f"{images} watermarked images for this chat. Credits never expire. "
                       f"All purchases are final - see /terms.",
        "payload": payload_for(pack),
        "currency": "XTR",  # Telegram Stars: no provider_token
        "prices": [{"label": f"{images} images", "amount": stars}],
    })


def answer_pre_checkout_query(bot_token, query_id, error=None):
    payload = {"pre_checkout_query_id": query_id, "ok": error is None}
    if error:
        payload["error_message"] = error
    _call(bot_token, "answerPreCheckoutQuery", payload)


def refund_star_payment(bot_token, user_id, charge_id):
    """Reverse a Stars payment. Takes the payer's user id, not the chat id."""
    _call(bot_token, "refundStarPayment", {"user_id": int(user_id), "telegram_payment_charge_id": charge_id})


def webhook_accepts_payments(bot_token):
    """Whether the webhook asks Telegram for pre_checkout_query updates. Without them
    every checkout is cancelled after 10 seconds and nothing reaches the logs."""
    allowed = _call(bot_token, "getWebhookInfo", {}).get("allowed_updates") or []
    # An empty list means Telegram's default set, which excludes nothing we need
    return not allowed or "pre_checkout_query" in allowed


# --- receiver side: must be quick ---

def pre_checkout_error(query, quota_available):
    """Why this payment must not go ahead, or None to accept it."""
    if not quota_available:
        return "Purchases aren't available right now. Nothing was charged."
    pack = pack_for_payload(query.get("invoice_payload"))
    if pack is None:
        return "This invoice is no longer valid. Send /buy for a new one. Nothing was charged."
    if query.get("currency") != "XTR" or query.get("total_amount") != PACKS[pack]["stars"]:
        return "This invoice's price has changed. Send /buy for a new one. Nothing was charged."
    return None


def handle_pre_checkout(bot_token, query, quota):
    error = pre_checkout_error(query, quota is not None)
    user = query.get("from", {}).get("id")
    print(f"payment pre-checkout user={user} payload={query.get('invoice_payload')} "
          f"amount={query.get('total_amount')} {'rejected: ' + error if error else 'accepted'}")
    answer_pre_checkout_query(bot_token, query["id"], error)


def handle_successful_payment(bot_token, message, quota):
    """Credit a completed payment. Raises if the credit couldn't be stored, so the
    receiver can answer 500 and Telegram delivers the payment again."""
    payment = message["successful_payment"]
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", chat_id)
    charge_id = payment["telegram_payment_charge_id"]
    pack = pack_for_payload(payment.get("invoice_payload"))

    if pack is None or quota is None or payment.get("total_amount") != PACKS[pack]["stars"]:
        # Pre-checkout accepts none of these, so seeing one means something is badly wrong
        print(f"PAYMENT NOT CREDITED chat={chat_id} user={user_id} charge={charge_id} "
              f"payload={payment.get('invoice_payload')} amount={payment.get('total_amount')} "
              f"quota={'on' if quota else 'off'}")
        # This condition is permanent, so the notice must not raise: a 500 would have Telegram
        # redeliver the payment forever. The line above is what the operator needs anyway.
        try:
            _send(bot_token, chat_id, "Your payment went through, but it couldn't be added to your balance "
                                      f"automatically. Please email {SUPPORT_EMAIL} and it will be put right.")
        except Exception as e:
            print(f"PAYMENT NOT CREDITED and the notice failed chat={chat_id} charge={charge_id}: {e}")
        return

    images = PACKS[pack]["images"]
    if quota.credit(chat_id, user_id, charge_id, pack, images, payment["total_amount"]):
        # The credit is stored: a failed thank-you must not make Telegram deliver the
        # payment again, since the retry would be a duplicate and get no message at all
        try:
            _send(bot_token, chat_id, f"Thank you! {images} images were added to this chat. "
                                      f"You now have {quota.balance(chat_id)} bought images.")
        except Exception as e:
            print(f"payment credited but the confirmation failed chat={chat_id} charge={charge_id}: {e}")


# --- worker side: the chat commands ---

def _balance_text(quota, chat_id):
    status = quota.status(chat_id)
    # The chat id is what support needs to find a purchase; it's the user's own, so safe to show
    chat_line = f"\nYour chat id: {chat_id} (include it if you email /paysupport)."
    if status["unlimited"]:
        return "This chat has no personal limit." + chat_line
    return (f"Free images left this month: {status['free_left']} of {status['free_monthly']} "
            f"(resets at {status['resets']}).\n"
            f"Bought images: {status['balance']}.\n"
            "Free images are used first. Buy more with /buy." + chat_line)


def handle_command(bot_token, chat_id, text, quota):
    """The payment commands. True if text was one of them."""
    command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if command == "/terms":
        _send(bot_token, chat_id, TERMS_TEXT)
    elif command == "/paysupport":
        _send(bot_token, chat_id, PAYSUPPORT_TEXT)
    elif command == "/balance":
        _send(bot_token, chat_id, "Image limits aren't enabled on this bot." if quota is None
              else _balance_text(quota, chat_id))
    elif command == "/buy":
        _buy(bot_token, chat_id, quota)
    elif command == "/usecredits":
        _use_credits(bot_token, chat_id, quota)
    else:
        return False
    return True


def _use_credits(bot_token, chat_id, quota):
    if quota is None or quota.is_unlimited(chat_id):
        _send(bot_token, chat_id, "This chat doesn't use bought images, so there's nothing to change.")
        return
    until = quota.use_credits_when_capped(chat_id)
    _send(bot_token, chat_id, "OK. Until " + until + ", if the bot's free images for the day are used up, "
                              "your bought images will be used instead. Send your zip again.")


def _buy(bot_token, chat_id, quota):
    if quota is None:
        _send(bot_token, chat_id, "Purchases aren't available on this bot.")
        return
    if quota.is_unlimited(chat_id):
        _send(bot_token, chat_id, "This chat has no personal limit, so there's nothing to buy.")
        return
    if not webhook_accepts_payments(bot_token):
        print("PAYMENTS BROKEN: the webhook doesn't request pre_checkout_query updates, so every checkout "
              "would be cancelled. Run 'serverless/deploy.sh attach' to set allowed_updates again.")
        _send(bot_token, chat_id, "Purchases are temporarily unavailable. Please try again later.")
        return
    _send(bot_token, chat_id, "Buy images for this chat. Your free monthly images are used first, and "
                              "bought images never expire. All purchases are final - see /terms.")
    for pack in PACKS:
        send_invoice(bot_token, chat_id, pack)
