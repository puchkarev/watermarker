# Watermarker Bot

A Telegram bot service that adds watermarks to images.

## Features

-   **Set Watermark**: Use `/source <url>` to set a watermark image for the current chat.
-   **Customize Position**: Use `/position` to place the watermark (9 positions or tiled).
-   **Customize Size**: Use `/size` to scale the watermark relative to the image.
-   **Auto-Watermark**: Send any image to the bot, and it will reply with the watermarked version.
-   **Per-Chat Config**: Each chat has its own watermark, position, and size settings.

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

## Usage

1.  Start a chat with the bot.
2.  **Set Watermark**: `/source https://example.com/my_logo.png`
3.  **Set Position**: `/position top left` (or top, top right, left, center, right, bottom left, bottom, bottom right, repeated)
4.  **Set Size**: `/size 0.1` (sets watermark to 10% of image width)
5.  **Get Help**: `/help`
6.  **Apply**: Send an image (photo) to the bot.

## Testing

Run unit tests with:

```bash
source venv/bin/activate
python3 test_watermarker.py
```
