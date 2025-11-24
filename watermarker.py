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

WATERMARKS_DIR = "watermarks"
TEMP_DIR = "temp"

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

def ensure_dirs():
    if not os.path.exists(WATERMARKS_DIR):
        os.makedirs(WATERMARKS_DIR)
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)

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

def apply_watermark(base_image_path, watermark_path, output_path):
    try:
        base = Image.open(base_image_path).convert("RGBA")
        watermark = Image.open(watermark_path).convert("RGBA")

        # Calculate watermark size (25% of base image width)
        target_width = int(base.width * 0.25)
        aspect_ratio = watermark.height / watermark.width
        target_height = int(target_width * aspect_ratio)
        
        watermark_resized = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)
        
        # Position: Bottom Right with some padding (e.g., 5% of width)
        # Or just exactly in the corner? "bottom right corner" usually implies some margin or flush.
        # "taking 25% of the image" - I'm using 25% width. 
        # I'll put it flush in the corner or with small padding? I'll use flush for simplicity unless it looks bad.
        # Let's add 10px padding or 2% padding.
        padding = int(base.width * 0.02)
        x = base.width - target_width - padding
        y = base.height - target_height - padding
        
        # Paste watermark
        # Create a transparent layer for the watermark
        transparent = Image.new('RGBA', base.size, (0, 0, 0, 0))
        transparent.paste(watermark_resized, (x, y))
        
        # Composite
        result = Image.alpha_composite(base, transparent)
        
        # Save as original format if possible, but RGBA forces PNG usually. 
        # If original was JPG, we might want to convert back to RGB.
        if output_path.lower().endswith(".jpg") or output_path.lower().endswith(".jpeg"):
            result = result.convert("RGB")
        
        result.save(output_path)
        return True
    except Exception as e:
        print(f"Error applying watermark: {e}")
        return False

def main():
    ensure_dirs()
    config = load_config()
    bot_token = config.get("bot_token")
    if not bot_token:
        print("Error: bot_token not found in config.json")
        return

    print("Bot started...")
    last_update_id = 0
    

def process_text(bot_token, chat_id, text):
    if text.startswith("/source"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            url = parts[1]
            if set_watermark(chat_id, url):
                tele.send_telegram(bot_token, str(chat_id), "Watermark set successfully!")
            else:
                tele.send_telegram(bot_token, str(chat_id), "Failed to set watermark. Check URL.")
        else:
            tele.send_telegram(bot_token, str(chat_id), "Usage: /source <url>")

def process_photo(bot_token, chat_id, photo_list):
    watermark_path = get_watermark_path(chat_id)
    if os.path.exists(watermark_path):
        # Get largest photo
        photo = photo_list[-1]
        file_id = photo["file_id"]
        
        # Download
        filename = tele.get_telegram_file(bot_token, str(chat_id), file_id, TEMP_DIR)
        if filename:
            local_path = os.path.join(TEMP_DIR, filename)
            output_path = os.path.join(TEMP_DIR, f"watermarked_{filename}")
            
            if apply_watermark(local_path, watermark_path, output_path):
                tele.send_telegram_file(bot_token, str(chat_id), output_path)
            else:
                tele.send_telegram(bot_token, str(chat_id), "Error processing image.")
            
            # Cleanup
            if os.path.exists(local_path):
                os.remove(local_path)
            if os.path.exists(output_path):
                os.remove(output_path)
    else:
        tele.send_telegram(bot_token, str(chat_id), "No watermark set. Use /source <url> to set one.")

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
