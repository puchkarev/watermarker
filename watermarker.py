import sys
import os
import errno
import json
import time
import ipaddress
from urllib.parse import urlparse
import requests
import zipfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from PIL import Image

# Add submodule to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'submodules', 'telegram'))
import tele

# Import core logic
from watermarker_core import apply_watermark, SUPPORTED_EXTS, DEFAULT_QUALITY

WATERMARKS_DIR = "watermarks"
TEMP_DIR = "temp"
SETTINGS_DIR = "settings"

# Commands shown in Telegram's command menu (also used by the serverless deployment)
BOT_COMMANDS = {
    "start": "Reset settings and show welcome message",
    "settings": "Show current watermark settings",
    "source": "Set the watermark image URL",
    "position": "Set watermark position",
    "size": "Set watermark size (fraction of original watermark width)",
    "strength": "Set watermark opacity (0.0 - 1.0)",
    "angle": "Set watermark rotation angle (0-360)",
    "mode": "Set watermark blending mode",
    "resize_8mp": "Toggle 8MP resize for output images",
    "x_offset": "Set horizontal watermark spacing",
    "y_offset": "Set vertical watermark spacing",
    "quality": "Set output image quality (1-100)",
    "help": "Show available commands"
}

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

def ensure_dirs():
    if not os.path.exists(WATERMARKS_DIR):
        os.makedirs(WATERMARKS_DIR)
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
    if not os.path.exists(SETTINGS_DIR):
        os.makedirs(SETTINGS_DIR)

def get_settings_path(chat_id):
    return os.path.join(SETTINGS_DIR, f"{chat_id}.json")

def load_settings(chat_id):
    path = get_settings_path(chat_id)
    defaults = {
        "position": "repeated", 
        "size": 2.0,
        "angle": 45,
        "mode": "negate",
        "strength": 0.2,
        "resize_8mp": True,
        "x_offset": 3.0,
        "y_offset": 3.0,
        "quality": DEFAULT_QUALITY
    }
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception as e:
            print(f"Error loading settings for {chat_id}: {e}")
    return defaults

def save_settings(chat_id, settings):
    try:
        with open(get_settings_path(chat_id), "w") as f:
            json.dump(settings, f)
    except Exception as e:
        print(f"Error saving settings for {chat_id}: {e}")

def _reset_settings(chat_id):
    path = get_settings_path(chat_id)
    if os.path.exists(path):
        os.remove(path)
        print(f"Settings for {chat_id} reset.")

def get_watermark_path(chat_id):
    return os.path.join(WATERMARKS_DIR, f"{chat_id}.png")

def _active_watermark_path(chat_id):
    """The chat's own watermark, else the default sun.webp. None if neither exists."""
    for path in (get_watermark_path(chat_id), "sun.webp"):
        if os.path.exists(path):
            return path
    return None

def _watermark_options(settings):
    """apply_watermark keyword arguments for a chat's settings."""
    return {
        "position": settings.get("position", "repeated"),
        "size": settings.get("size", 2.0),
        "strength": settings.get("strength", 0.2),
        "angle": settings.get("angle", 45),
        "mode": settings.get("mode", "negate"),
        "max_pixels": 8000000 if settings.get("resize_8mp", True) else None,
        "x_offset": settings.get("x_offset", 3.0),
        "y_offset": settings.get("y_offset", 3.0),
        "quality": settings.get("quality", DEFAULT_QUALITY),
    }

def _is_image_name(file_name):
    return os.path.splitext(file_name)[1].lower() in SUPPORTED_EXTS

def _is_os_junk(rel_path):
    """Metadata files macOS/Windows add to zips (e.g. __MACOSX/._photo.jpg), not real images."""
    parts = rel_path.replace("\\", "/").split("/")
    return ("__MACOSX" in parts or parts[-1].startswith("._")
            or parts[-1] in (".DS_Store", "Thumbs.db", "desktop.ini"))

def _describe_error(e):
    """Turn an exception into a message suitable for sending back to the user."""
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "the server is out of storage space"
    return f"{type(e).__name__}: {e}"

def _download_file(bot_token, file_id):
    """Download a Telegram file into TEMP_DIR and return its local path. Raises on failure."""
    response = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile",
                            params={"file_id": file_id}, timeout=30).json()
    if not response.get("ok"):
        raise RuntimeError(f"Telegram refused the download: {response.get('description', 'unknown error')}")

    file_path = response["result"]["file_path"]
    local_path = os.path.join(TEMP_DIR, f"{int(time.time() * 1000)}{os.path.splitext(file_path)[1]}")
    with requests.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}", stream=True, timeout=300) as r:
        r.raise_for_status()
        try:
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        except Exception:
            if os.path.exists(local_path):
                os.remove(local_path)
            raise
    return local_path

def _send_document(bot_token, chat_id, path, file_name, caption=""):
    """Upload a file as a document under the given name. Raises on failure."""
    with open(path, "rb") as f:
        response = requests.post(f"https://api.telegram.org/bot{bot_token}/sendDocument",
                                 data={"chat_id": str(chat_id), "caption": caption},
                                 files={"document": (file_name, f)}, timeout=600).json()
    if not response.get("ok"):
        raise RuntimeError(f"Telegram refused the upload: {response.get('description', 'unknown error')}")

def _is_safe_url(url):
    """Check that a URL doesn't point to internal/private network addresses."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        import socket
        resolved = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in resolved:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return False
        return True
    except Exception:
        return False

def set_watermark(chat_id, url):
    try:
        if not _is_safe_url(url):
            print(f"Blocked unsafe URL: {url}")
            return False

        response = requests.get(url, stream=True, timeout=15)
        response.raise_for_status()
        return _save_watermark(chat_id, response.content)
    except Exception as e:
        print(f"Error setting watermark: {e}")
        return False

def _save_watermark(chat_id, data):
    """Validate image bytes and store them as the chat's watermark. Returns True on success."""
    try:
        # Verify it's an image
        img = Image.open(BytesIO(data))
        img.verify()

        # Re-open to save (verify consumes the stream/file)
        img = Image.open(BytesIO(data))
        img.save(get_watermark_path(chat_id))
        return True
    except Exception as e:
        print(f"Error setting watermark: {e}")
        return False

def _set_number(bot_token, chat_id, text, key, low, high, usage, cast=float):
    """Handle a '/command <number>' that stores a number within [low, high] in the chat's settings."""
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        tele.send_telegram(bot_token, str(chat_id), usage)
        return
    try:
        value = cast(parts[1])
    except ValueError:
        tele.send_telegram(bot_token, str(chat_id), "Invalid number format.")
        return
    if not low <= value <= high:
        tele.send_telegram(bot_token, str(chat_id), f"{key} must be between {low} and {high}")
        return
    settings = load_settings(chat_id)
    settings[key] = value
    save_settings(chat_id, settings)
    tele.send_telegram(bot_token, str(chat_id), f"{key} set to: {value}")

def process_text(bot_token, chat_id, text):
    text = text.strip()

    help_text = (
        "Welcome to Watermarker Bot!\n\n"
        "GitHub Repo: https://github.com/puchkarev/watermarker\n\n"
        "I can apply watermarks to your images. Send me a photo directly or a ZIP file containing multiple images. "
        "I'll process them and send them back to you as files, converting output images to WebP (max 8MP by default, or as configured). "
        "Photos sent as files, and in zips, keep their full quality.\n\n"
        "Available commands:\n"
        "/start - Reset your chat settings to default and show this welcome message.\n"
        "/settings - Show your current watermark settings.\n"
        "/source <url> - Set the watermark image for this chat from a direct image URL (PNG, JPG, etc.). "
        "Or send an image with the caption /source (send it as a file to keep transparency).\n"
        "/position <pos> - Set watermark position. Options: top left, top, top right, left, center, right, bottom left, bottom, bottom right, repeated.\n"
        "/size <fraction> - Set watermark size as fraction of watermark's original width (0.1 for 10%, 0.5 for 50%).\n"
        "/strength <fraction> - Set watermark opacity (0.0 - 1.0). Default: 0.2.\n"
        "/angle <degrees> - Set watermark rotation angle in degrees (0-360). Default: 45.\n"
        "/mode <mode> - Set watermark blending mode. Options: standard, difference, negate. Default: negate.\n"
        "/resize_8mp <true/false> - Enable/disable resizing output images to max 8 megapixels. Default: true.\n"
        "/x_offset <multiple> - Horizontal spacing as a multiple of the watermark width (1.0 = no gap). Default: 3.0.\n"
        "/y_offset <multiple> - Vertical spacing as a multiple of the watermark height (1.0 = no gap). Default: 3.0.\n"
        f"/quality <1-100> - Output image quality. Higher is sharper but larger. Default: {DEFAULT_QUALITY}.\n"
        "/help - Show this message."
    )
    
    if text.startswith("/help"):
        tele.send_telegram(bot_token, str(chat_id), help_text)
    
    elif text.startswith("/start"):
        _reset_settings(chat_id)
        # Send welcome message after reset
        tele.send_telegram(bot_token, str(chat_id), help_text)
    
    elif text.startswith("/settings"):
        settings = load_settings(chat_id)
        settings_text = "Current Settings:\n"
        for key, value in settings.items():
            settings_text += f"- {key}: {value}\n"
        tele.send_telegram(bot_token, str(chat_id), settings_text)
        
    elif text.startswith("/source"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            url = parts[1]
            if set_watermark(chat_id, url):
                tele.send_telegram(bot_token, str(chat_id), "Watermark set successfully!")
            else:
                tele.send_telegram(bot_token, str(chat_id), "Failed to set watermark. Check URL.")
        else:
            tele.send_telegram(bot_token, str(chat_id),
                               "Usage: /source <url>, or send an image with the caption /source")
            
    elif text.startswith("/position"):
        parts = text.split(maxsplit=1)
        valid_positions = [
            "top left", "top", "top right", 
            "left", "center", "right", 
            "bottom left", "bottom", "bottom right", 
            "repeated"
        ]
        
        if len(parts) == 2:
            pos = parts[1].lower().strip()
            if pos in valid_positions:
                settings = load_settings(chat_id)
                settings["position"] = pos
                save_settings(chat_id, settings)
                tele.send_telegram(bot_token, str(chat_id), f"Position set to: {pos}")
            else:
                tele.send_telegram(bot_token, str(chat_id), f"Invalid position. Valid: {', '.join(valid_positions)}")
        else:
            tele.send_telegram(bot_token, str(chat_id), f"Usage: /position <pos>\nValid: {', '.join(valid_positions)}")
            
    elif text.startswith("/size"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                size_val = float(parts[1])
                if 0.0 < size_val <= 10.0:
                    settings = load_settings(chat_id)
                    settings["size"] = size_val
                    save_settings(chat_id, settings)
                    tele.send_telegram(bot_token, str(chat_id), f"Size set to: {size_val}")
                else:
                    tele.send_telegram(bot_token, str(chat_id), "Size must be between 0.0 and 10.0")
            except ValueError:
                tele.send_telegram(bot_token, str(chat_id), "Invalid number format.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /size <fraction> (e.g., 0.25)")

    elif text.startswith("/strength"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                strength_val = float(parts[1])
                if 0.0 <= strength_val <= 1.0:
                    settings = load_settings(chat_id)
                    settings["strength"] = strength_val
                    save_settings(chat_id, settings)
                    tele.send_telegram(bot_token, str(chat_id), f"Strength set to: {strength_val}")
                else:
                    tele.send_telegram(bot_token, str(chat_id), "Strength must be between 0.0 and 1.0")
            except ValueError:
                tele.send_telegram(bot_token, str(chat_id), "Invalid number format.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /strength <fraction> (e.g., 0.5)")

    elif text.startswith("/angle"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                angle_val = float(parts[1])
                if 0.0 <= angle_val <= 360.0:
                    settings = load_settings(chat_id)
                    settings["angle"] = angle_val
                    save_settings(chat_id, settings)
                    tele.send_telegram(bot_token, str(chat_id), f"Angle set to: {angle_val}")
                else:
                    tele.send_telegram(bot_token, str(chat_id), "Angle must be between 0.0 and 360.0")
            except ValueError:
                tele.send_telegram(bot_token, str(chat_id), "Invalid number format.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /angle <degrees> (e.g., 45)")

    elif text.startswith("/mode"):
        parts = text.split(maxsplit=1)
        valid_modes = ["standard", "difference", "negate"]
        if len(parts) == 2:
            mode_val = parts[1].lower().strip()
            if mode_val in valid_modes:
                settings = load_settings(chat_id)
                settings["mode"] = mode_val
                save_settings(chat_id, settings)
                tele.send_telegram(bot_token, str(chat_id), f"Mode set to: {mode_val}")
            else:
                tele.send_telegram(bot_token, str(chat_id), f"Invalid mode. Valid: {', '.join(valid_modes)}")
        else:
            tele.send_telegram(bot_token, str(chat_id), f"Usage: /mode <mode>\nValid: {', '.join(valid_modes)}")

    elif text.startswith("/resize_8mp"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            resize_val = parts[1].lower().strip()
            if resize_val in ["true", "false"]:
                settings = load_settings(chat_id)
                settings["resize_8mp"] = (resize_val == "true")
                save_settings(chat_id, settings)
                tele.send_telegram(bot_token, str(chat_id), f"Resize to 8MP set to: {settings['resize_8mp']}")
            else:
                tele.send_telegram(bot_token, str(chat_id), "Invalid value. Use 'true' or 'false'.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /resize_8mp <true/false>")

    elif text.startswith("/x_offset"):
        _set_number(bot_token, chat_id, text, "x_offset", 0.1, 10.0, "Usage: /x_offset <multiple> (e.g., 3.0)")

    elif text.startswith("/y_offset"):
        _set_number(bot_token, chat_id, text, "y_offset", 0.1, 10.0, "Usage: /y_offset <multiple> (e.g., 3.0)")

    elif text.startswith("/quality"):
        _set_number(bot_token, chat_id, text, "quality", 1, 100, "Usage: /quality <1-100> (e.g., 90)", cast=int)

NO_WATERMARK_MESSAGE = "No watermark set and default 'sun.webp' not found. Use /source <url> to set one."

def _worker_count():
    # Pillow releases the GIL while decoding, resizing and encoding, so threads run in parallel
    return max(1, min(4, os.cpu_count() or 1))

def process_document(bot_token, chat_id, document):
    file_name = document.get("file_name", "")
    mime_type = document.get("mime_type", "")
    file_id = document["file_id"]

    if mime_type == "application/zip" or file_name.lower().endswith(".zip"):
        _process_zip(bot_token, chat_id, file_id, file_name)
    elif _is_image_name(file_name) or mime_type.startswith("image/"):
        # Images sent as files keep full quality; reply with a file of the same name, as WebP
        stem = os.path.splitext(os.path.basename(file_name))[0] or "image"
        _process_single_image(bot_token, chat_id, file_id, ".webp", f"{stem}.webp")
    else:
        tele.send_telegram(bot_token, str(chat_id),
                           "Unsupported file type. Send a photo, an image file, or a .zip of images.")

def _process_zip(bot_token, chat_id, file_id, file_name):
    watermark_path = _active_watermark_path(chat_id)
    if not watermark_path:
        tele.send_telegram(bot_token, str(chat_id), NO_WATERMARK_MESSAGE)
        return

    tele.send_telegram(bot_token, str(chat_id), "Processing zip file... this may take a moment.")

    # Reply with the same name the user sent
    result_name = os.path.basename(file_name) or "watermarked.zip"
    if not result_name.lower().endswith(".zip"):
        result_name += ".zip"

    zip_path = None
    extract_dir = processed_dir = result_zip_path = None

    try:
        zip_path = _download_file(bot_token, file_id)
        zip_filename = os.path.basename(zip_path)
        extract_dir = os.path.join(TEMP_DIR, f"extract_{zip_filename}")
        processed_dir = os.path.join(TEMP_DIR, f"processed_{zip_filename}")
        result_zip_path = os.path.join(TEMP_DIR, f"watermarked_{zip_filename}")

        os.makedirs(extract_dir, exist_ok=True)
        os.makedirs(processed_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Protect against zip-slip: reject entries with absolute paths or '..'
            for member in zip_ref.namelist():
                member_path = os.path.realpath(os.path.join(extract_dir, member))
                if not member_path.startswith(os.path.realpath(extract_dir) + os.sep) and member_path != os.path.realpath(extract_dir):
                    raise ValueError(f"Zip entry with unsafe path: {member}")
            zip_ref.extractall(extract_dir)

        options = _watermark_options(load_settings(chat_id))

        # Collect the work: every image, keeping the zip's folder structure, converted to webp
        jobs = []
        skipped = []
        for root, dirs, files in os.walk(extract_dir):
            for filename in sorted(files):
                input_path = os.path.join(root, filename)
                rel_path = os.path.relpath(input_path, extract_dir)
                if _is_os_junk(rel_path):
                    continue
                if not _is_image_name(filename):
                    skipped.append(rel_path)
                    continue
                output_path = os.path.join(processed_dir, os.path.splitext(rel_path)[0] + ".webp")
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                jobs.append((input_path, rel_path, output_path))

        def run(job):
            input_path, _, output_path = job
            return apply_watermark(input_path, watermark_path, output_path, **options)

        with ThreadPoolExecutor(max_workers=_worker_count()) as pool:
            results = list(pool.map(run, jobs))

        processed_count = results.count(True)
        failed = [rel_path for (_, rel_path, _), ok in zip(jobs, results) if not ok]
        skipped_note = f" Skipped {len(skipped)} non-image file(s)." if skipped else ""

        if processed_count > 0:
            # Zip result
            shutil.make_archive(os.path.splitext(result_zip_path)[0], 'zip', processed_dir)

            # Send result under the original name
            caption = f"Watermarked {processed_count} image(s)."
            if failed:
                caption += f" {len(failed)} failed."
            caption += skipped_note
            _send_document(bot_token, chat_id, result_zip_path, result_name, caption)

            if failed:
                tele.send_telegram(bot_token, str(chat_id),
                                   f"Could not process {len(failed)} image(s):\n" + "\n".join(failed))
        elif failed:
            tele.send_telegram(bot_token, str(chat_id),
                               f"All {len(failed)} image(s) in the zip failed to process:\n" + "\n".join(failed))
        else:
            tele.send_telegram(bot_token, str(chat_id), "No images found in zip." + skipped_note)

    except Exception as e:
        print(f"Error processing zip: {e}")
        tele.send_telegram(bot_token, str(chat_id), f"Error processing zip file: {_describe_error(e)}")
    finally:
        # Cleanup
        if zip_path and os.path.exists(zip_path): os.remove(zip_path)
        if extract_dir and os.path.exists(extract_dir): shutil.rmtree(extract_dir)
        if processed_dir and os.path.exists(processed_dir): shutil.rmtree(processed_dir)
        if result_zip_path and os.path.exists(result_zip_path): os.remove(result_zip_path)

def _process_single_image(bot_token, chat_id, file_id, output_ext, result_name):
    """Watermark one image and send it back as a file, so Telegram doesn't recompress it."""
    watermark_path = _active_watermark_path(chat_id)
    if not watermark_path:
        tele.send_telegram(bot_token, str(chat_id), NO_WATERMARK_MESSAGE)
        return

    local_path = output_path = None
    try:
        local_path = _download_file(bot_token, file_id)
        output_path = os.path.join(TEMP_DIR, f"watermarked_{os.path.splitext(os.path.basename(local_path))[0]}{output_ext}")

        options = _watermark_options(load_settings(chat_id))
        if apply_watermark(local_path, watermark_path, output_path, **options):
            _send_document(bot_token, chat_id, output_path, result_name)
        else:
            tele.send_telegram(bot_token, str(chat_id), "Error processing image.")
    except Exception as e:
        print(f"Error processing photo: {e}")
        tele.send_telegram(bot_token, str(chat_id), f"Error processing image: {_describe_error(e)}")
    finally:
        # Cleanup
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
        if output_path and os.path.exists(output_path):
            os.remove(output_path)

def process_photo(bot_token, chat_id, photo_list):
    # Largest size Telegram kept; it was already compressed when sent as a photo
    file_id = photo_list[-1]["file_id"]
    _process_single_image(bot_token, chat_id, file_id, ".jpg", "watermarked.jpg")

def process_source_upload(bot_token, chat_id, message):
    """An image sent with the caption /source becomes the chat's watermark."""
    if "document" in message:
        file_id = message["document"]["file_id"]
    else:
        file_id = message["photo"][-1]["file_id"]

    local_path = None
    try:
        local_path = _download_file(bot_token, file_id)
        with open(local_path, "rb") as f:
            ok = _save_watermark(chat_id, f.read())
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)

    if ok:
        tele.send_telegram(bot_token, str(chat_id), "Watermark set successfully!")
    else:
        tele.send_telegram(bot_token, str(chat_id), "Failed to set watermark: that file isn't an image.")

def handle_update(bot_token, update):
    if "message" not in update:
        return
        
    message = update["message"]
    chat_id = message["chat"]["id"]

    try:
        # An image captioned /source sets the watermark instead of being watermarked
        caption = (message.get("caption") or "").strip()
        if caption.startswith("/source") and ("photo" in message or "document" in message):
            process_source_upload(bot_token, chat_id, message)
            return

        # Handle Text Commands
        if "text" in message:
            process_text(bot_token, chat_id, message["text"])

        # Handle Photos
        if "photo" in message:
            process_photo(bot_token, chat_id, message["photo"])

        # Handle Documents (zips and image files)
        if "document" in message:
            process_document(bot_token, chat_id, message["document"])
    except Exception as e:
        # Never fail silently: tell the user what went wrong
        print(f"Error handling update for {chat_id}: {e}")
        tele.send_telegram(bot_token, str(chat_id), f"Error: {_describe_error(e)}")

def main():
    ensure_dirs()
    config = load_config()
    bot_token = config.get("bot_token")
    if not bot_token:
        print("Error: bot_token not found in config.json")
        return
    
    # Set bot commands
    tele.telegram_set_commands(bot_token, BOT_COMMANDS)

    print("Bot started...")
    last_update_id = 0
    
    while True:
        try:
            updates = tele.get_telegram_updates(bot_token, last_update_id)
            if "result" in updates:
                for update in updates["result"]:
                    last_update_id = update["update_id"]
                    handle_update(bot_token, update)

            time.sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()