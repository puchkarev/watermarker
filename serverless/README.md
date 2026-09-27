# Serverless deployment (AWS Lambda, on demand)

This folder runs the same bot as `watermarker.py`, but only while it is actually
being used. Nothing runs between messages, so a bot that handles one batch of
photos a week costs effectively nothing (it stays inside the AWS free tier).

The regular deployment (`scripts/deploy.sh`, an always-on polling process on a
server) is unchanged. You can use either one, or both with two different bots.

## How it works

```
Telegram ──POST──▶ Lambda Function URL ──▶ receiver: checks the webhook secret, answers 200 at once
                                              │
                                              └─ async invoke ─▶ worker: watermarker.handle_update()
                                                                   downloads the photo/zip into /tmp,
                                                                   watermarks it, sends it back
```

- **One function, two roles.** The receiver has to answer Telegram quickly, or
  Telegram re-sends the message and the zip would be processed twice. So it hands
  the message to a second, asynchronous run of the same function and returns.
- **Each zip is processed in its own run**, in parallel. A batch of 20 zips
  finishes in about the time one zip takes.
- **Nothing is stored except per-chat configuration and image counts.** Photos and zips only live
  in the function's `/tmp` during one run. The settings you set with `/size`,
  `/angle`, etc. and the image from `/source` are kept as tiny files in a private
  S3 bucket (`settings/<chat id>.json`, `watermarks/<chat id>.png`), because a
  Lambda keeps no disk between runs. The only other data is the image
  counters for the quotas (see step 6).
- **The bot code is not forked.** `lambda_function.py` calls the same
  `handle_update()` from `watermarker.py`, so every command and fix applies to
  both deployments.

### What gets created in your AWS account

| Resource | Purpose |
|---|---|
| Lambda function `watermarker-bot` | Runs the bot (Python 3.12, ARM, 2 GB memory, 15 min timeout) |
| Lambda Function URL | Public HTTPS address Telegram sends messages to. Requests without the secret are refused. |
| S3 bucket | Per-chat settings and watermark images (a few KB) |
| DynamoDB table | Image counters for the quotas (old months and days delete themselves), bought credits and purchase records (kept) |
| IAM role | Lets the function use that bucket, write logs and call itself |
| CloudWatch log group | Logs, kept for 14 days |

All of it lives in one CloudFormation stack, so it can be removed in one step.

### Limits

- **Files from Telegram can be at most 20 MB** (a Telegram Bot API limit that also
  applies to the regular deployment). Split larger batches into several zips.
- Results sent back can be at most 50 MB each.
- One zip has 15 minutes to finish. At 2 GB memory, 10–20 photos take well under a
  minute. Raise it with `--memory` if you send very large photos.

### Cost

About 20 runs a week of roughly 30 seconds at 2 GB is around 5,000 GB-seconds a
month. The Lambda free tier includes 400,000 GB-seconds and 1 million requests
a month, and S3 storage for a few KB rounds to $0.00. Function URLs have no charge.

## Step by step

### 1. Pick where to run the deploy script

The script needs `bash`, the AWS CLI v2, `python3` with `pip`, `curl` and `zip`.
Any of these works:

- **AWS CloudShell (easiest).** Open the AWS console, click the terminal icon
  (`>_`) in the top bar. It already has every tool and is logged in to your account,
  so you can skip step 2.
- **macOS / Linux.** Install the AWS CLI v2:
  <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>.
  The other tools are usually already there (Debian/Ubuntu:
  `sudo apt install python3-pip zip curl`).
- **Windows.** Use WSL (Ubuntu) and follow the Linux instructions.

The machine you deploy from can be any OS or CPU: the script downloads Linux ARM
packages for Lambda regardless.

### 2. Give the AWS CLI access to your account (skip in CloudShell)

1. In the AWS console, open **IAM → Users → Create user**. Give it a name such as
   `watermarker-deployer`.
2. Attach permissions. The simplest is the `AdministratorAccess` policy. If you
   prefer something narrower, it needs CloudFormation, Lambda, IAM (roles), S3
   and CloudWatch Logs.
3. Open the user, go to **Security credentials → Create access key → Command Line
   Interface**, and copy the key id and secret.
4. Run:
   ```bash
   aws configure
   # AWS Access Key ID:     <key id>
   # AWS Secret Access Key: <secret>
   # Default region name:   us-east-1   (or the region closest to you)
   # Default output format: json
   ```
5. Check it works: `aws sts get-caller-identity` prints your account number.

### 3. Get a bot token

**A bot token can be used by only one deployment at a time.** When the Lambda sets
its webhook, Telegram stops giving messages to a polling copy of `watermarker.py`
that uses the same token, and that copy starts logging errors. So choose:

- **Move your existing bot to Lambda.** Stop the server version first
  (`sudo systemctl stop watermarker`) and use its token. Your users keep the same bot.
- **Run both side by side.** Create a second bot for the Lambda: message
  [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts.
  It replies with a token like `123456789:AA...`.

You can move between the two later with `deploy.sh detach` / `deploy.sh attach`
(see [Switching](#switching-between-lambda-and-a-server)).

### 4. Get the code

```bash
git clone --recursive https://github.com/puchkarev/watermarker.git
cd watermarker
```

If you cloned without `--recursive`, run `git submodule update --init`. The
script also tries to do this for you.

### 5. Deploy

```bash
./serverless/deploy.sh deploy
```

It asks for the bot token (input is hidden), or you can pass it:

```bash
./serverless/deploy.sh deploy --bot-token 123456789:AA... --region us-east-1
```

What the script does:

1. Checks the tools and your AWS login.
2. Builds `serverless/build/watermarker-lambda.zip` with the bot code, the default
   `sun.webp` watermark and the Linux ARM builds of Pillow, requests and
   python-telegram-bot.
3. Creates the stack from `template.yaml`. The first time this takes 1–3 minutes.
   It also generates a random webhook secret.
4. Uploads the code to the function.
5. Grants the Function URL permission to invoke the function.
6. Checks the Function URL answers, then calls Telegram's `setWebhook` with the
   URL and the secret, and sets the bot's command menu.

When it prints `Done`, send the bot `/help`, then a photo or a zip.

### 6. Image limits

The Function URL is public so that Telegram can reach it, and anyone who finds
your bot in Telegram can use it on your AWS account. Quotas keep that
bounded while leaving the bot open:

| Who | Limit |
|---|---|
| Chats you list with `--unlimited-chat-ids` | no personal limit |
| Everyone else | 10 images per UTC calendar month (`--free-monthly-images`) |
| The whole bot, all chats together | 5000 images per UTC day (`--system-daily-images`) |

- One photo counts 1 and a zip of 20 images counts 20. Commands and `/source` are free.
- Only images that are delivered count. Failed images are handed back.
- A zip bigger than what's left is refused whole and nothing is used up; the
  reply gives the zip's image count and how many are left. A zip bigger than the
  whole allowance is told to split it, since it would never fit. The reply always
  says whether it was the person's own limit (resets at 00:00 UTC on the 1st of
  the month) or the bot's overall limit (resets at 00:00 UTC each day).
- The counters live in a small DynamoDB table (free tier at this volume). Old
  rows delete themselves once their month or day is over.

To make your own chats unlimited:

1. Find your chat id: send the bot a photo, then run `./serverless/deploy.sh logs`
   and look for `quota chat=<id>`. Group chats have negative ids.
2. Redeploy with your id(s), comma-separated:
   ```bash
   ./serverless/deploy.sh deploy --unlimited-chat-ids 123456789,-1001234567890
   ```

Change the limits the same way, e.g. `--free-monthly-images 20 --system-daily-images 2000`.
`--free-daily-images` was replaced by `--free-monthly-images` when the free
allowance became monthly; the old flag now stops with an error pointing at the new one.
Every request is logged with the chat id, its image count and the running totals,
so `logs` doubles as usage accounting.

The old `--allowed-chat-ids` flag still works for one more release as a hard
allowlist: every other chat is refused, the listed chats are treated as unlimited
(as they were before quotas), and the function logs a deprecation line while it's set. Clear it with `--allowed-chat-ids ""` once you've moved to
`--unlimited-chat-ids`.

### 7. Selling more images (Telegram Stars)

Once a chat has used its free images it can buy more with `/buy`, paid in
Telegram Stars. Nothing needs setting up: Stars need no payment provider, and
the deploy script already asks Telegram for the `pre_checkout_query` updates
checkout needs.

| Pack | Images | Price |
|---|---|---|
| Small | 100 | 200 Stars |
| Large | 1000 | 1000 Stars |

- The free monthly images are used first, then bought ones. Bought images never
  expire and aren't limited by the bot's daily cap.
- A zip needing more than free + bought images is refused whole, with how many it's
  short. Failed images go back to where they came from, bought ones first.
- On a day the bot's free images run out, a chat that still has free images left
  isn't switched to bought ones without asking: it is told so, and `/usecredits`
  allows it for the rest of that day.
- `/balance` shows both, `/terms` summarises [TERMS.md](../TERMS.md) (all purchases
  are final), and `/paysupport` gives the billing contact.
- Each payment is credited exactly once, even when Telegram delivers it twice, and
  the payer's user id is kept with it. Payments are handled by the receiver
  directly: if storing one fails, Telegram is asked to deliver it again.
- `grep PAYMENT` in the logs finds anything that went wrong with a payment.

If you deployed before payments existed, run `./serverless/deploy.sh deploy`
(or `attach`) once: an older webhook doesn't receive `pre_checkout_query`, and
Telegram would cancel every checkout. `/buy` checks for that and says purchases
are unavailable rather than taking anyone to a checkout that can't finish.

### 8. Admin commands

Chats listed with `--admin-chat-ids` get a set of admin commands; send `/admin`
in one of them to list them. This is a separate list from `--unlimited-chat-ids`,
so adding a friend to the unlimited list never makes them an admin. Use private
chats: every member of an admin group chat would be an admin.

```bash
./serverless/deploy.sh deploy --admin-chat-ids 123456789
```

| Command | What it does |
|---|---|
| `/users [page]` | Chats with any usage or balance, most active this month first |
| `/usage <chat id>` | One chat: this month, free used, bought images left, lifetime total, recent purchases |
| `/charges <chat id> [page]` | A chat's purchases, with the charge ids `/refund` needs |
| `/limits` | The configured limits and how much of today's free cap is used |
| `/stats` | Totals: images this month, active chats, images sold and Stars taken |
| `/grant <chat id> <images> [reason]` | Add images to a chat's balance. No money moves; prefer this for goodwill or to fix a billing mistake |
| `/refund <charge id>` | Show what refunding a purchase would do; `/refund <charge id> confirm` does it |

- A refund sends the Stars back to the person who paid, takes the pack's images
  back (never below zero), and can only happen once per purchase. The preview says
  if the pack's images were already used, counting the oldest purchases first, and
  so how many would come out of later purchases.
- If Telegram refuses, nothing changes. If it gives no clear answer (a timeout, a
  server error), the purchase is marked "refund pending" and nothing else happens;
  confirming again finishes it safely, since Telegram never pays a charge twice.
- `/grant` and `/refund` each write an `ADMIN AUDIT` line to the logs:
  `./serverless/deploy.sh logs | grep "ADMIN AUDIT"`.
- For anyone else these commands behave like any unknown command: no reply, and
  nothing in the logs.
- Refunds aren't offered to users (all purchases are final, see
  [TERMS.md](../TERMS.md)); `/refund` exists to put right a billing mistake.

## Day-to-day commands

| Command | What it does |
|---|---|
| `./serverless/deploy.sh deploy` | Rebuild and redeploy (after `git pull`). Keeps the token, secret, limits, unlimited chats and all chat settings. |
| `./serverless/deploy.sh status` | Show the webhook URL, bucket, image limits, unlimited and admin chats and Telegram's webhook status, including the last error Telegram saw |
| `./serverless/deploy.sh logs` | Follow the function's logs live (Ctrl+C to stop) |
| `./serverless/deploy.sh detach` | Remove the webhook, for example to hand the token back to a server |
| `./serverless/deploy.sh attach` | Set the webhook again |
| `./serverless/deploy.sh remove` | Remove the webhook and delete the stack (asks first). The settings bucket and the quota table are kept, and the script prints the commands to delete them. |

Every command accepts `--stack-name NAME` (default `watermarker-bot`) and
`--region REGION`. Use a different stack name to run more than one bot.

To change the bot token: `./serverless/deploy.sh deploy --bot-token NEW_TOKEN`.

## Switching between Lambda and a server

- **Lambda → server:** `./serverless/deploy.sh detach`, then start the server
  version (`sudo systemctl start watermarker`). Messages sent in between are
  kept by Telegram and picked up by the server.
- **Server → Lambda:** stop the server version, then `./serverless/deploy.sh attach`.

Chat settings are not shared: the server keeps them in its `settings/` folder,
and Lambda keeps them in S3.

## Troubleshooting

- **The bot doesn't answer.** Run `./serverless/deploy.sh status` and look at
  `last_error_message` in Telegram's webhook info, then `./serverless/deploy.sh logs`.
  - `Wrong response from the webhook: 403 Forbidden`: the webhook secret doesn't
    match. Run `./serverless/deploy.sh attach` to set it again.
  - `url` is empty: the webhook was removed. Run `./serverless/deploy.sh attach`.
  - Nothing in the logs and no error from Telegram: check that the message was
    sent to the bot this stack uses (`status` shows the webhook URL Telegram has).
- **`could not add the Function URL invoke permission`.** Your AWS CLI is too old
  for the `--invoked-via-function-url` option. Update it and run `deploy` again.
- **`the function URL is not reachable yet`.** New Function URLs can take a
  minute to start working. Run `./serverless/deploy.sh attach`.
- **A big zip fails with "file is too big".** That is Telegram's 20 MB limit for
  bots. Split the batch into smaller zips.
- **A zip times out** (logs show `Task timed out after 900 seconds`). Redeploy
  with more memory, which also gives more CPU, for example `--memory 4096`.
  Failed runs are not retried, so you never receive duplicate results.

## Files

| File | Purpose |
|---|---|
| `lambda_function.py` | Lambda handler: webhook receiver, async worker, S3 sync of chat settings |
| `template.yaml` | CloudFormation template for every AWS resource |
| `deploy.sh` | Build, deploy and management script |
| `requirements.txt` | Python packages bundled into the Lambda |
| `quota.py` | Free allowance, system cap, bought credits and purchase records in DynamoDB |
| `payments.py` | Telegram Stars: invoices, pre-checkout, crediting, `/buy`, `/balance`, `/usecredits`, `/terms`, `/paysupport` |
| `admin.py` | Admin commands for the chats in `ADMIN_CHAT_IDS` |
| `test_lambda_function.py`, `test_quota.py`, `test_payments.py`, `test_admin.py` | Tests, run each with `python serverless/<file>` |
