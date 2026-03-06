import sys
import os
import json
import time
import ipaddress
from urllib.parse import urlparse
import requests
import zipfile
import shutil
from io import BytesIO
from PIL import Image

# Add submodule to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'submodules', 'telegram'))
import tele

# Import core logic
from watermarker_core import apply_watermark

WATERMARKS_DIR = "watermarks"
TEMP_DIR = "temp"
SETTINGS_DIR = "settings"

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
        "resize_8mp": True
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

        # Verify it's an image
        img = Image.open(BytesIO(response.content))
        img.verify()

        # Save it
        # Re-open to save (verify consumes the stream/file)
        img = Image.open(BytesIO(response.content))
        img.save(get_watermark_path(chat_id))
        return True
    except Exception as e:
        print(f"Error setting watermark: {e}")
        return False

def process_text(bot_token, chat_id, text):
    text = text.strip()

    help_text = (
        "Welcome to Watermarker Bot!\n\n"
        "GitHub Repo: https://github.com/puchkarev/watermarker\n\n"
        "I can apply watermarks to your images. Send me a photo directly or a ZIP file containing multiple images. "
        "I'll process them and send them back to you, converting all output images to WebP (max 8MP by default for single images, or as configured).\n\n"
        "Available commands:\n"
        "/start - Reset your chat settings to default and show this welcome message.\n"
        "/settings - Show your current watermark settings.\n"
        "/source <url> - Set the watermark image for this chat. Supports direct image URLs (PNG, JPG, etc.).\n"
        "/position <pos> - Set watermark position. Options: top left, top, top right, left, center, right, bottom left, bottom, bottom right, repeated.\n"
        "/size <fraction> - Set watermark size as fraction of watermark's original width (0.1 for 10%, 0.5 for 50%).\n"
        "/strength <fraction> - Set watermark opacity (0.0 - 1.0). Default: 0.2.\n"
        "/angle <degrees> - Set watermark rotation angle in degrees (0-360). Default: 45.\n"
        "/mode <mode> - Set watermark blending mode. Options: standard, difference, negate. Default: negate.\n"
        "/resize_8mp <true/false> - Enable/disable resizing output images to max 8 megapixels. Default: true.\n"
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

def process_document(bot_token, chat_id, document):
    file_name = document.get("file_name", "")
    mime_type = document.get("mime_type", "")
    file_id = document["file_id"]
    
    if mime_type == "application/zip" or file_name.lower().endswith(".zip"):
        tele.send_telegram(bot_token, str(chat_id), "Processing zip file... this may take a moment.")
        
        # Download zip
        zip_filename = tele.get_telegram_file(bot_token, str(chat_id), file_id, TEMP_DIR)
        if not zip_filename:
            return

        zip_path = os.path.join(TEMP_DIR, zip_filename)
        extract_dir = os.path.join(TEMP_DIR, f"extract_{zip_filename}")
        processed_dir = os.path.join(TEMP_DIR, f"processed_{zip_filename}")
        result_zip_path = os.path.join(TEMP_DIR, f"watermarked_{zip_filename}")

        try:
            os.makedirs(extract_dir, exist_ok=True)
            os.makedirs(processed_dir, exist_ok=True)

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Protect against zip-slip: reject entries with absolute paths or '..'
                for member in zip_ref.namelist():
                    member_path = os.path.realpath(os.path.join(extract_dir, member))
                    if not member_path.startswith(os.path.realpath(extract_dir) + os.sep) and member_path != os.path.realpath(extract_dir):
                        raise ValueError(f"Zip entry with unsafe path: {member}")
                zip_ref.extractall(extract_dir)
            
            # Get settings
            watermark_path = get_watermark_path(chat_id)
            if not os.path.exists(watermark_path):
                watermark_path = "sun.webp"
            
            if not os.path.exists(watermark_path):
                tele.send_telegram(bot_token, str(chat_id), "No watermark available.")
                return

            settings = load_settings(chat_id)
            position = settings.get("position", "repeated")
            size = settings.get("size", 2.0)
            strength = settings.get("strength", 0.2)
            angle = settings.get("angle", 45)
            mode = settings.get("mode", "negate")
            resize_8mp = settings.get("resize_8mp", True)
            max_pixels = 8000000 if resize_8mp else None

            # Process files
            supported_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
            processed_count = 0
            
            for root, dirs, files in os.walk(extract_dir):
                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in supported_exts:
                        input_path = os.path.join(root, filename)
                        
                        # Calculate relative path to maintain structure if needed, 
                        # but usually flattening or keeping structure is choice.
                        # Let's flatten for simplicity in processed_dir or match structure?
                        # Matching structure is safer for "unzip, process, zip".
                        
                        rel_path = os.path.relpath(input_path, extract_dir)
                        # Change ext to webp
                        rel_path_webp = os.path.splitext(rel_path)[0] + ".webp"
                        output_path = os.path.join(processed_dir, rel_path_webp)
                        
                        # Ensure output dir exists
                        os.makedirs(os.path.dirname(output_path), exist_ok=True)
                        
                        if apply_watermark(input_path, watermark_path, output_path, 
                                           position=position, size=size, strength=strength,
                                           angle=angle, mode=mode, max_pixels=max_pixels):
                            processed_count += 1

            if processed_count > 0:
                # Zip result
                shutil.make_archive(os.path.splitext(result_zip_path)[0], 'zip', processed_dir)
                
                # Send result
                tele.send_telegram_file(bot_token, str(chat_id), result_zip_path)
            else:
                tele.send_telegram(bot_token, str(chat_id), "No images found or processed in zip.")

        except Exception as e:
            print(f"Error processing zip: {e}")
            tele.send_telegram(bot_token, str(chat_id), "Error processing zip file.")
        finally:
            # Cleanup
            if os.path.exists(zip_path): os.remove(zip_path)
            if os.path.exists(extract_dir): shutil.rmtree(extract_dir)
            if os.path.exists(processed_dir): shutil.rmtree(processed_dir)
            if os.path.exists(result_zip_path): os.remove(result_zip_path)

def process_photo(bot_token, chat_id, photo_list):
    watermark_path = get_watermark_path(chat_id)
    if not os.path.exists(watermark_path):
        # Fallback to default
        watermark_path = "sun.webp"
        
    # Check if watermark (specific or default) exists
    if os.path.exists(watermark_path):
        # Get largest photo
        photo = photo_list[-1]
        file_id = photo["file_id"]
        
        # Download
        filename = tele.get_telegram_file(bot_token, str(chat_id), file_id, TEMP_DIR)
        if filename:
            local_path = os.path.join(TEMP_DIR, filename)
            output_path = os.path.join(TEMP_DIR, f"watermarked_{filename}")
            
            settings = load_settings(chat_id)
            # Unpack settings for the core function
            position = settings.get("position", "repeated")
            size = settings.get("size", 2.0)
            strength = settings.get("strength", 0.2)
            angle = settings.get("angle", 45)
            mode = settings.get("mode", "negate")
            resize_8mp = settings.get("resize_8mp", True)
            max_pixels = 8000000 if resize_8mp else None
            
            if apply_watermark(local_path, watermark_path, output_path, position=position, size=size, strength=strength, angle=angle, mode=mode, max_pixels=max_pixels):
                tele.send_telegram_file(bot_token, str(chat_id), output_path)
            else:
                tele.send_telegram(bot_token, str(chat_id), "Error processing image.")
            
            # Cleanup
            if os.path.exists(local_path):
                os.remove(local_path)
            if os.path.exists(output_path):
                os.remove(output_path)
    else:
        tele.send_telegram(bot_token, str(chat_id), "No watermark set and default 'sun.webp' not found. Use /source <url> to set one.")

def handle_update(bot_token, update):
    if "message" not in update:
        return
        
    message = update["message"]
    chat_id = message["chat"]["id"]
    
    # Handle Text Commands
    if "text" in message:
        process_text(bot_token, chat_id, message["text"])
    
    # Handle Photos
    if "photo" in message:
        process_photo(bot_token, chat_id, message["photo"])

    # Handle Documents (Zip)
    if "document" in message:
        process_document(bot_token, chat_id, message["document"])

def main():
    ensure_dirs()
    config = load_config()
    bot_token = config.get("bot_token")
    if not bot_token:
        print("Error: bot_token not found in config.json")
        return
    
    # Set bot commands
    commands = {
        "start": "Reset settings and show welcome message",
        "settings": "Show current watermark settings",
        "source": "Set the watermark image URL",
        "position": "Set watermark position",
        "size": "Set watermark size (fraction of original watermark width)",
        "strength": "Set watermark opacity (0.0 - 1.0)",
        "angle": "Set watermark rotation angle (0-360)",
        "mode": "Set watermark blending mode",
        "resize_8mp": "Toggle 8MP resize for output images",
        "help": "Show available commands"
    }
    tele.telegram_set_commands(bot_token, commands)

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