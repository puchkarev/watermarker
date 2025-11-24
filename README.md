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

## CLI Usage

The core watermarking logic can be used independently as a command-line tool via `watermarker_core.py`.

### Single Image
```bash
python3 watermarker_core.py input.jpg watermark.png output.jpg --position "top right" --size 0.2
```

### Batch Processing
Process an entire folder of images. This mode automatically converts outputs to **WebP** format.
```bash
python3 watermarker_core.py ./input_folder ./watermark.png ./output_folder
```

### Options
- `--position`: Where to place the watermark (e.g., `center`, `bottom right`, `repeated`). Default: `bottom right`.
- `--size`: Size of watermark as a fraction of image width (0.0 - 1.0). Default: `0.25`.
- `--resize-8mp`: Resize output images to a maximum of 8 megapixels (approx 3266x2449) if the input is larger. Maintains aspect ratio.
- `--mode`: Blending mode.
    - `standard`: Normal overlay (alpha blending). Default.
    - `difference`: Calculates absolute difference. Good for high contrast visibility on any background.
    - `negate`: Inverts the background image color where the watermark is present.
- `--angle`: Rotation angle of the watermark in degrees (counter-clockwise). Default: `0`.

```bash
# Example: Batch process, resize to 8MP, 15% watermark size, using difference mode and 45 degree rotation
python3 watermarker_core.py ./raw_photos ./logo.png ./processed --size 0.15 --resize-8mp --mode difference --angle 45
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
