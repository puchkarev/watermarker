# Watermarker Bot

[![CI](https://github.com/puchkarev/watermarker/actions/workflows/ci.yml/badge.svg)](https://github.com/puchkarev/watermarker/actions/workflows/ci.yml)

A Telegram bot service that adds watermarks to images.

The bot runs here [bot](https://t.me/add_sun_watermark_bot)

## Features

-   **Set Watermark**: Use `/source <url>`, or send an image with the caption `/source`, to set a watermark image for the current chat.
-   **Customize Position**: Use `/position` to place the watermark (9 positions or tiled).
-   **Customize Size**: Use `/size` to scale the watermark relative to the image.
-   **Customize Angle**: Use `/angle` to rotate the watermark.
-   **Customize Mode**: Use `/mode` to change blending mode.
-   **Customize Strength**: Use `/strength` to set opacity.
-   **Customize Spacing**: Use `/x_offset` and `/y_offset` to set the gap between tiled watermarks (or the margin from the edge).
-   **Customize Quality**: Use `/quality` to set the output image quality (1-100).
-   **Toggle Resize**: Use `/resize_8mp` to enable/disable automatic image resizing.
-   **Auto-Watermark**: Send a photo or an image file to the bot, and it will reply with the watermarked version as a file (so Telegram doesn't recompress it). Image files come back as WebP under the same name.
-   **Zip File Processing**: Send a `.zip` file containing images, and the bot will process all images in parallel (converting to WebP, 8MP max) and return a new `.zip` file with the same name. Non-image files and macOS/Windows metadata files are left out.
-   **Photo-Friendly**: Camera rotation (EXIF orientation) is applied so portrait shots stay upright, colour profiles (e.g. Adobe RGB) are kept, and iPhone HEIC photos are supported.
-   **Per-Chat Config**: Each chat has its own watermark, position, and size settings.
-   **Reset Settings**: Use `/start` to reset chat settings to defaults.
-   **View Settings**: Use `/settings` to see current configuration.

## Installation

1.  Clone the repository and submodules:
    ```bash
    git clone --recursive <repo_url>
    ```
2.  Install dependencies:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

### Automated Deployment

For production environments, you can use the provided deployment script to fetch the latest nightly release, set up the environment, and manage the service.

1.  **Prerequisites**: The script requires `unzip`, `curl`, and `python3`. It will attempt to install them (asking for `sudo` permission) if they are missing.
2.  **Run**:
    ```bash
    # Basic usage (prompts for setup if needed)
    ./scripts/deploy.sh

    # With GitHub Token (for private repos) and Bot Token (for auto-config)
    ./scripts/deploy.sh --token YOUR_GITHUB_PAT --bot-token YOUR_TELEGRAM_BOT_TOKEN

    # Custom installation directory and service name
    ./scripts/deploy.sh --dir /var/www/watermarker --service my-bot-service
    ```

    The script will:
    - Download the latest release.
    - Create the installation directory.
    - Prompt for your Bot Token and create `config.json` if missing (or use `--bot-token`).
    - Create and enable a systemd service if one doesn't exist.
    - Restart the service.

### Serverless Deployment (AWS Lambda, on demand)

If the bot is only used occasionally, it can instead run on AWS Lambda. It only runs while it handles a message, so a bot used for one batch a week costs effectively nothing (it stays inside the AWS free tier). It uses the same bot code, and nothing is stored except each chat's settings and daily image counts.

**Before you start:** one bot token can only be used by one deployment at a time. Once the Lambda is connected, a server copy of the bot using the same token stops receiving messages. Either stop the server version first (`sudo systemctl stop watermarker`), or create a second bot for the Lambda with [@BotFather](https://t.me/BotFather) (`/newbot`).

**Quick start (AWS CloudShell):**

1.  Sign in to the AWS console and open **CloudShell** (the `>_` icon in the top bar). It already has every tool the script needs and is logged in to your account.
2.  Run:
    ```bash
    git clone --recursive https://github.com/puchkarev/watermarker.git
    cd watermarker
    ./serverless/deploy.sh deploy --region us-east-1
    ```
3.  Paste your bot token when asked. The script builds the package, creates the AWS resources, uploads the code, and connects your Telegram bot to it.
4.  When it prints `Done`, send the bot `/help`, then a photo or a zip.

To deploy from your own machine instead, install the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), `python3` with `pip`, `curl` and `zip`, and run `aws configure` first.

**Image limits:** anyone can use the bot, but each chat gets 10 free images a month and the whole bot 5000 a day, so strangers can't run up your AWS bill. Make your own chats unlimited (find your chat id with `./serverless/deploy.sh logs`, looking for `quota chat=<id>`):
```bash
./serverless/deploy.sh deploy --unlimited-chat-ids 123456789
```

**Managing it:**

| Command | What it does |
|---|---|
| `./serverless/deploy.sh deploy` | Build and deploy, or redeploy after a `git pull`. Keeps the token, limits and chat settings. |
| `./serverless/deploy.sh status` | Show the setup and whether Telegram can reach it |
| `./serverless/deploy.sh logs` | Follow the bot's logs live |
| `./serverless/deploy.sh detach` / `attach` | Hand the bot token to a server copy and back |
| `./serverless/deploy.sh remove` | Delete the AWS resources (the small settings bucket is kept; the script prints how to delete it) |

**Limits:** Telegram only lets bots download files up to 20 MB, so split larger batches into several zips. Each zip gets up to 15 minutes to process.

See [serverless/README.md](serverless/README.md) for the full guide: creating AWS credentials, what gets created in your account, costs, switching between the server and Lambda, and troubleshooting.

The server deployment above (`scripts/deploy.sh`) also works on an AWS EC2 Linux instance if you prefer an always-on bot.

## Configuration

Ensure `config.json` exists in the root directory with your Telegram Bot Token:

```json
{
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN"
}
```

## Running the Bot

```bash
source venv/bin/activate
python3 watermarker.py
```

## CLI Usage

The core watermarking logic can be used independently as a command-line tool via `watermarker_core.py`.

### Single Image
```bash
python3 watermarker_core.py input.jpg watermark.png output.jpg --position "top right" --size 0.5
```

### Batch Processing
Process an entire folder of images. This mode automatically converts outputs to **WebP** format.
```bash
python3 watermarker_core.py ./input_folder ./watermark.png ./output_folder
```

### Options
- `--position`: Where to place the watermark (e.g., `center`, `bottom right`). Use `repeated` to tile the watermark across the entire image. Default: `repeated`.
- `--size`: Size of watermark as a fraction of watermark's original width (0.0 - 1.0). Default: `2.0`.
- `--resize-8mp`: Resize output images to a maximum of 8 megapixels (approx 3266x2449) if the input is larger. Maintains aspect ratio. Default: `True`.
- `--mode`: Blending mode.
    - `standard`: Normal overlay (alpha blending).
    - `difference`: Calculates absolute difference. Good for high contrast visibility on any background.
    - `negate`: Inverts the background image color where the watermark is present. Default: `negate`.
- `--angle`: Rotation angle of the watermark in degrees (counter-clockwise). Default: `45`.
- `--strength`: Opacity/Strength of the watermark (0.0 - 1.0). Default: `0.2`.
- `--x-offset` / `--y-offset`: Spacing as a multiple of the watermark's width/height (1.0 = no gap). Default: `1.0`.
- `--quality`: JPEG/WebP output quality (1 - 100). Default: `80`.

```bash
# Example: Batch process, resize to 8MP, 15% watermark size, using difference mode and 45 degree rotation
python3 watermarker_core.py ./raw_photos ./logo.png ./processed --size 0.15 --resize-8mp --mode difference --angle 45 --strength 0.8
```

## Usage

1.  Start a chat with the bot. A `/start` command will reset your settings to default and greet you.
2.  **Set Watermark**: `/source https://example.com/my_logo.png`, or send an image with the caption `/source` (send it as a file to keep a transparent background)
3.  **Customize Position**: `/position top left` (or top, top right, left, center, right, bottom left, bottom, bottom right, repeated)
4.  **Customize Size**: `/size 0.1` (sets watermark to 10% of its original width)
5.  **Customize Strength**: `/strength 0.5` (sets watermark opacity to 50%)
6.  **Customize Angle**: `/angle 90` (rotates watermark 90 degrees counter-clockwise)
7.  **Customize Mode**: `/mode difference` (sets blending mode)
8.  **Toggle Resize**: `/resize_8mp false` (disables 8MP resizing)
9.  **Customize Spacing**: `/x_offset 2.0` and `/y_offset 2.0` (gap between tiles as a multiple of the watermark size; 1.0 = no gap)
10. **Customize Quality**: `/quality 90` (higher is sharper but larger files; default 80)
11. **View Settings**: `/settings` (shows current configuration)
12. **Get Help**: `/help`
13. **Apply**: Send a photo, an image file, or a **.zip file containing images** (all images will be watermarked, converted to WebP, and resized to 8MP if `resize_8mp` is true). The bot replies with the watermarked file or a processed zip file. Send images as files rather than photos for full quality.

## Testing

Run unit tests with:

```bash
source venv/bin/activate
python3 test_watermarker.py
python3 serverless/test_lambda_function.py
```
