# Terms and Conditions

**Service:** the Watermarker bot, [@add_sun_watermark_bot](https://t.me/add_sun_watermark_bot) on Telegram
**Source code:** <https://github.com/puchkarev/watermarker>
**Last updated:** 2026-09-26

By sending the bot an image, a file or a payment, you agree to these terms. If you
do not agree, do not use the bot.

## 1. What the service does

You send the bot photos or a `.zip` of images; it applies a watermark and sends the
results back. Position, size, angle, opacity, spacing, output quality and the
watermark image itself are configurable per chat. See `/help` in the bot.

The service is operated by the bot's owner as a personal project. It is not a
company, and it carries no service-level guarantee.

## 2. Free allowance and paid credits

Each chat gets **10 images per calendar month at no charge**, counted in UTC and
reset at the start of each month.

Beyond that you can buy **image credits** in packs:

| Pack | Images | Price |
|---|---|---|
| Small | 100 | 200 Telegram Stars |
| Large | 1000 | 1000 Telegram Stars |

- One photo costs one image. A zip of 20 images costs 20 images.
- Commands, settings changes and setting a watermark with `/source` are free.
- Your free monthly allowance is used first; paid credits are used only after it
  runs out.
- Credits are a count of images, not a balance of Stars. **Credits do not expire.**
- Credits are tied to the chat that bought them. They cannot be transferred,
  resold, or exchanged for money or Stars.
- Images that fail to process, or results that never reach you, do not consume your
  allowance or credits.
- If a zip needs more images than you have available, the whole zip is refused and
  nothing is charged. Split it into smaller zips or buy more credits.

## 3. Payment

Payments are processed by **Telegram** using Telegram Stars. The bot never sees or
stores your payment method. Buying Stars is a transaction between you, Telegram,
and your app store; these terms cover only what the bot gives you in return.

Prices may change. A change never affects credits you have already bought.

## 4. Refunds

- **Unused packs:** if you have bought a pack and used none of its images, you may
  request a full refund. Contact `/paysupport`.
- **Partly used packs:** these are not automatically refundable, because the work
  has already been done. Contact `/paysupport` and it will be looked at case by
  case.
- **Our failures:** if the bot takes your credits and does not deliver, the credits
  are returned automatically. If that does not happen, contact `/paysupport` and
  the credits will be restored or the payment refunded.

Refunds of Stars are issued through Telegram and returned to your Telegram Stars
balance, not to your card.

## 5. Limits

These are technical limits, not policy, and mostly come from Telegram:

- Files you send must be **20 MB or smaller** — a Telegram limit for bots.
- Results returned are at most **50 MB** each.
- Each zip gets **15 minutes** to process.
- Output images are resized to a maximum of 8 megapixels by default. Turn that off
  with `/resize_8mp false`.
- Free usage across all users is capped at 5000 images per day. Paid credits are
  not affected by that cap.

## 6. Your images and your rights

- You must own the images you send, or otherwise have the right to modify them.
- You keep all rights to your images. The owner claims no ownership of anything you
  send or of the watermarked results.
- You are responsible for what you send and for how you use the results.

## 7. Acceptable use

Do not use the bot to process material that is illegal, that you have no right to
use, or that is designed to harm others. Do not attempt to bypass the allowance,
the credit system, or the rate limits, and do not use the bot to attack or overload
the service. Access may be withdrawn for any of these, without a refund of credits
bought in bad faith.

## 8. Data and privacy

**Images are not kept.** Photos and zips exist only in temporary storage for the
duration of one request and are deleted when it finishes. They are never stored in
a database, never used to train anything, and never shared.

What *is* stored:

| Data | Where | For how long |
|---|---|---|
| Your chat's settings (position, size, angle, etc.) | Private cloud storage | Until you change or reset them |
| A watermark image you set with `/source` | Private cloud storage | Until you replace it |
| Your free-usage count and credit balance | Database, keyed by chat id | Usage counts expire automatically; credits persist |
| Operational logs, including your chat id | Cloud logs | **14 days**, then deleted automatically |
| Payment records (Telegram's charge id, amount, pack) | Database | Kept as a record of purchase |

The bot runs on Amazon Web Services in the US East region, so data is processed
there. To have your stored settings, watermark and balance deleted, ask via
`/paysupport` or email <victor.puchkarev@gmail.com>. Note that deleting a balance
forfeits unused credits.

## 9. Availability and changes

The bot is provided as-is, with no guarantee of availability. It may be changed,
interrupted, or discontinued at any time. If it is discontinued permanently,
holders of unused credits will be offered a refund through Telegram where Telegram
still permits it.

## 10. No warranty and limitation of liability

The service is provided **"as is", without warranty of any kind**, express or
implied, including fitness for a particular purpose. The owner is not liable for
any loss or damage arising from use of the bot, including lost or corrupted images,
missed deadlines, or unavailability.

To the extent the law allows, total liability for any claim is limited to the
amount you paid in the 30 days before the claim, or the value of your unused
credits, whichever is greater.

Nothing here removes rights you have under consumer law that cannot be waived.

## 11. Support and disputes

Use `/paysupport` in the bot for anything about payments, credits or refunds, and
`/terms` to see this document. `/paysupport` reaches:

**Victor Puchkarev — <victor.puchkarev@gmail.com>**

Please include which pack you bought and roughly when, so the purchase can be found.

For bugs and feature requests, the public issue tracker is
<https://github.com/puchkarev/watermarker/issues>. **Do not post payment details,
receipts or personal information in a public issue** — use `/paysupport` for
anything involving a purchase.

## 12. Changes to these terms

These terms may change. The current version is always the one at
<https://github.com/puchkarev/watermarker/blob/main/TERMS.md>, with the date at the
top. Continuing to use the bot after a change means you accept it.

## 13. Governing law

These terms are governed by the laws of the **State of California, United States**,
without regard to its conflict-of-laws rules. Any dispute that cannot be settled
through `/paysupport` will be handled in the state or federal courts located in
California.

Nothing in this section removes rights you have under the mandatory consumer law of
your own country of residence.
