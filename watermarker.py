import sys
import os
import json
import time
import requests
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
    defaults = {"position": "bottom right", "size": 0.25}
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

def get_watermark_path(chat_id):
    return os.path.join(WATERMARKS_DIR, f"{chat_id}.png")

def set_watermark(chat_id, url):
    try:
        response = requests.get(url, stream=True)
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
    
    if text.startswith("/help"):
        help_text = (
            "Available commands:\n"
            "/source <url> - Set the watermark image for this chat.\n"
            "/position <pos> - Set watermark position. Options: top left, top, top right, left, center, right, bottom left, bottom, bottom right, repeated.\n"
            "/size <fraction> - Set watermark size as fraction of watermark's original width (e.g. 0.1 for 10%, 0.5 for 50%).\n"
            "/help - Show this message."
        )
        tele.send_telegram(bot_token, str(chat_id), help_text)
        
    elif text.startswith("/source"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            url = parts[1]
            if set_watermark(chat_id, url):
                tele.send_telegram(bot_token, str(chat_id), "Watermark set successfully!")
            else:
                tele.send_telegram(bot_token, str(chat_id), "Failed to set watermark. Check URL.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /source <url>")
            
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
                if 0.0 < size_val <= 1.0:
                    settings = load_settings(chat_id)
                    settings["size"] = size_val
                    save_settings(chat_id, settings)
                    tele.send_telegram(bot_token, str(chat_id), f"Size set to: {size_val}")
                else:
                    tele.send_telegram(bot_token, str(chat_id), "Size must be between 0.0 and 1.0")
            except ValueError:
                tele.send_telegram(bot_token, str(chat_id), "Invalid number format.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /size <fraction> (e.g., 0.25)")

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
            position = settings.get("position", "bottom right")
            size = settings.get("size", 0.25)
            
            if apply_watermark(local_path, watermark_path, output_path, position=position, size=size):
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

def main():
    ensure_dirs()
    config = load_config()
    bot_token = config.get("bot_token")
    if not bot_token:
        print("Error: bot_token not found in config.json")
        return
    
    # Set bot commands
    commands = {
        "source": "Set the watermark image URL",
        "position": "Set watermark position",
        "size": "Set watermark size (fraction of original watermark width)",
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