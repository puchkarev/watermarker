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
- **Nothing is stored except per-chat configuration and daily counts.** Photos and zips only live
  in the function's `/tmp` during one run. The settings you set with `/size`,
  `/angle`, etc. and the image from `/source` are kept as tiny files in a private
  S3 bucket (`settings/<chat id>.json`, `watermarks/<chat id>.png`), because a
  Lambda keeps no disk between runs. The only other data is the daily image
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
| DynamoDB table | Daily image counters for the quotas; old days delete themselves |
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

### 6. Daily image limits

The Function URL is public so that Telegram can reach it, and anyone who finds
your bot in Telegram can use it on your AWS account. Daily quotas keep that
bounded while leaving the bot open:

| Who | Limit per UTC day |
|---|---|
| Chats you list with `--unlimited-chat-ids` | no personal limit |
| Everyone else | 10 images (`--free-daily-images`) |
| The whole bot, all chats together | 5000 images (`--system-daily-images`) |

- One photo counts 1 and a zip of 20 images counts 20. Commands and `/source` are free.
- Only images that are delivered count. Failed images are handed back.
- A zip bigger than what's left today is refused whole and nothing is used up;
  the reply gives the zip's image count and how many are left. A zip bigger than
  the whole daily allowance is told to split it, since it would never fit. The reply always says whether it was the person's own
  limit or the bot's overall limit, and when it resets (00:00 UTC).
- The counters live in a small DynamoDB table (free tier at this volume). Each
  day's rows delete themselves.

To make your own chats unlimited:

1. Find your chat id: send the bot a photo, then run `./serverless/deploy.sh logs`
   and look for `quota chat=<id>`. Group chats have negative ids.
2. Redeploy with your id(s), comma-separated:
   ```bash
   ./serverless/deploy.sh deploy --unlimited-chat-ids 123456789,-1001234567890
   ```

Change the limits the same way, e.g. `--free-daily-images 20 --system-daily-images 2000`.
Every request is logged with the chat id, its image count and the running totals,
so `logs` doubles as usage accounting.

The old `--allowed-chat-ids` flag still works for one more release as a hard
allowlist: every other chat is refused, the listed chats are treated as unlimited
(as they were before quotas), and the function logs a deprecation line while it's set. Clear it with `--allowed-chat-ids ""` once you've moved to
`--unlimited-chat-ids`.

## Day-to-day commands

| Command | What it does |
|---|---|
| `./serverless/deploy.sh deploy` | Rebuild and redeploy (after `git pull`). Keeps the token, secret, limits, unlimited chats and all chat settings. |
| `./serverless/deploy.sh status` | Show the webhook URL, bucket, daily limits, unlimited chats and Telegram's webhook status, including the last error Telegram saw |
| `./serverless/deploy.sh logs` | Follow the function's logs live (Ctrl+C to stop) |
| `./serverless/deploy.sh detach` | Remove the webhook, for example to hand the token back to a server |
| `./serverless/deploy.sh attach` | Set the webhook again |
| `./serverless/deploy.sh remove` | Remove the webhook and delete the stack (asks first). The settings bucket is kept, and the script prints the command to delete it. |

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
| `test_lambda_function.py` | Tests, run with `python serverless/test_lambda_function.py` |
