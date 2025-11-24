# Watermarker Bot

[![CI](https://github.com/puchkarev/watermarker/actions/workflows/ci.yml/badge.svg)](https://github.com/puchkarev/watermarker/actions/workflows/ci.yml)

A Telegram bot service that adds watermarks to images.

## Features

-   **Set Watermark**: Use `/source <url>` to set a watermark image for the current chat.
-   **Customize Position**: Use `/position` to place the watermark (9 positions or tiled).
-   **Customize Size**: Use `/size` to scale the watermark relative to the image.
-   **Customize Angle**: Use `/angle` to rotate the watermark.
-   **Customize Mode**: Use `/mode` to change blending mode.
-   **Customize Strength**: Use `/strength` to set opacity.
-   **Toggle Resize**: Use `/resize_8mp` to enable/disable automatic image resizing.
-   **Auto-Watermark**: Send any image to the bot, and it will reply with the watermarked version.
-   **Zip File Processing**: Send a `.zip` file containing images, and the bot will process all images (converting to WebP, 8MP max) and return a new `.zip` file.
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

```bash
# Example: Batch process, resize to 8MP, 15% watermark size, using difference mode and 45 degree rotation
python3 watermarker_core.py ./raw_photos ./logo.png ./processed --size 0.15 --resize-8mp --mode difference --angle 45 --strength 0.8
```

## Usage

1.  Start a chat with the bot. A `/start` command will reset your settings to default and greet you.
2.  **Set Watermark**: `/source https://example.com/my_logo.png` (or send an image file directly)
3.  **Customize Position**: `/position top left` (or top, top right, left, center, right, bottom left, bottom, bottom right, repeated)
4.  **Customize Size**: `/size 0.1` (sets watermark to 10% of its original width)
5.  **Customize Strength**: `/strength 0.5` (sets watermark opacity to 50%)
6.  **Customize Angle**: `/angle 90` (rotates watermark 90 degrees counter-clockwise)
7.  **Customize Mode**: `/mode difference` (sets blending mode)
8.  **Toggle Resize**: `/resize_8mp false` (disables 8MP resizing)
9.  **View Settings**: `/settings` (shows current configuration)
10. **Get Help**: `/help`
11. **Apply**: Send an image (photo) to the bot, or a **.zip file containing images** (all images will be watermarked, converted to WebP, and resized to 8MP if `resize_8mp` is true). The bot will reply with the watermarked version or a processed zip file.

## Testing

Run unit tests with:

```bash
source venv/bin/activate
python3 test_watermarker.py
```
