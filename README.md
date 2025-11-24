# Watermarker Bot

A Telegram bot service that adds watermarks to images.

## Features

-   **Set Watermark**: Use `/source <url>` to set a watermark image for the current chat.
-   **Auto-Watermark**: Send any image to the bot, and it will reply with the watermarked version.
-   **Per-Chat Config**: Each chat can have its own unique watermark.
-   **Smart Resizing**: The watermark is automatically resized to 25% of the target image's width and placed in the bottom-right corner.

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
2.  Send `/source https://example.com/my_logo.png` to set your watermark.
3.  Send an image (photo) to the bot.
4.  Receive the watermarked image back.

## Testing

Run unit tests with:

```bash
source venv/bin/activate
python3 test_watermarker.py
```