import argparse
import sys
import os
from PIL import Image

def apply_watermark(base_image_path, watermark_path, output_path, position="bottom right", size=0.25, max_pixels=None):
    """
    Applies a watermark to an image.
    
    Args:
        base_image_path (str): Path to the base image.
        watermark_path (str): Path to the watermark image.
        output_path (str): Path to save the result.
        position (str): Position of the watermark. 
                        Options: 'top left', 'top', 'top right', 'left', 'center', 'right', 
                                 'bottom left', 'bottom', 'bottom right', 'repeated'.
        size (float): Size of the watermark as a fraction of the base image width (0.0 - 1.0).
        max_pixels (int): Maximum number of pixels for the output image. If input is larger, it will be resized.
    
    Returns:
        bool: True if successful, False otherwise.
    """
    try:
        base = Image.open(base_image_path).convert("RGBA")
        
        # Resize if max_pixels is set and image is larger
        if max_pixels:
            current_pixels = base.width * base.height
            if current_pixels > max_pixels:
                ratio = (max_pixels / current_pixels) ** 0.5
                new_width = int(base.width * ratio)
                new_height = int(base.height * ratio)
                # Ensure at least 1 pixel
                new_width = max(1, new_width)
                new_height = max(1, new_height)
                base = base.resize((new_width, new_height), Image.Resampling.LANCZOS)

        watermark = Image.open(watermark_path).convert("RGBA")

        # Calculate watermark size
        target_width = int(base.width * size)
        if target_width < 1: target_width = 1
        aspect_ratio = watermark.height / watermark.width
        target_height = int(target_width * aspect_ratio)
        if target_height < 1: target_height = 1
        
        watermark_resized = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)
        
        # Create a transparent layer for the watermark
        transparent = Image.new('RGBA', base.size, (0, 0, 0, 0))
        
        padding = int(base.width * 0.02)
        
        if position == "repeated":
            # Tile the watermark
            for y in range(0, base.height, target_height + padding):
                for x in range(0, base.width, target_width + padding):
                    transparent.paste(watermark_resized, (x, y))
        else:
            # Calculate coordinates
            # Default to bottom right
            x = base.width - target_width - padding
            y = base.height - target_height - padding
            
            if position == "top left":
                x = padding
                y = padding
            elif position == "top":
                x = (base.width - target_width) // 2
                y = padding
            elif position == "top right":
                x = base.width - target_width - padding
                y = padding
            elif position == "left":
                x = padding
                y = (base.height - target_height) // 2
            elif position == "center":
                x = (base.width - target_width) // 2
                y = (base.height - target_height) // 2
            elif position == "right":
                x = base.width - target_width - padding
                y = (base.height - target_height) // 2
            elif position == "bottom left":
                x = padding
                y = base.height - target_height - padding
            elif position == "bottom":
                x = (base.width - target_width) // 2
                y = base.height - target_height - padding
            elif position == "bottom right":
                x = base.width - target_width - padding
                y = base.height - target_height - padding

            transparent.paste(watermark_resized, (x, y))
        
        # Composite
        result = Image.alpha_composite(base, transparent)
        
        # Save logic
        # If output path implies a specific format, use it.
        # But if we want to ensure webp in batch mode, the extension is already set in output_path.
        
        if output_path.lower().endswith(".jpg") or output_path.lower().endswith(".jpeg"):
            result = result.convert("RGB")
        
        result.save(output_path)
        return True
    except Exception as e:
        print(f"Error applying watermark: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Apply a watermark to an image.")
    parser.add_argument("base_image", help="Path to the source image or directory")
    parser.add_argument("watermark_image", help="Path to the watermark image")
    parser.add_argument("output_image", help="Path to save the watermarked image or output directory")
    parser.add_argument("--position", default="bottom right", 
                        choices=['top left', 'top', 'top right', 'left', 'center', 'right', 
                                 'bottom left', 'bottom', 'bottom right', 'repeated'],
                        help="Position of the watermark")
    parser.add_argument("--size", type=float, default=0.25, help="Size fraction (0.0 - 1.0)")
    parser.add_argument("--resize-8mp", action="store_true", help="Resize output to approx 8 megapixels (maintain aspect ratio)")

    args = parser.parse_args()
    
    max_pixels = 8000000 if args.resize_8mp else None

    if os.path.isdir(args.base_image):
        # Batch processing
        if not os.path.exists(args.output_image):
            try:
                os.makedirs(args.output_image)
            except OSError as e:
                print(f"Error creating output directory: {e}")
                sys.exit(1)
                
        supported_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
        processed_count = 0
        
        for filename in os.listdir(args.base_image):
            ext = os.path.splitext(filename)[1].lower()
            if ext in supported_exts:
                input_path = os.path.join(args.base_image, filename)
                # Feature: Change encoding to webp for batch processing
                output_filename = os.path.splitext(filename)[0] + ".webp"
                output_path = os.path.join(args.output_image, output_filename)
                
                print(f"Processing {filename} -> {output_filename}...")
                if apply_watermark(input_path, args.watermark_image, output_path, args.position, args.size, max_pixels):
                    processed_count += 1
                else:
                    print(f"Failed to process {filename}")
        
        print(f"Batch processing complete. {processed_count} images processed.")
        sys.exit(0)

    else:
        # Single file processing
        if apply_watermark(args.base_image, args.watermark_image, args.output_image, args.position, args.size, max_pixels):
            print(f"Successfully saved to {args.output_image}")
            sys.exit(0)
        else:
            print("Failed to apply watermark")
            sys.exit(1)

if __name__ == "__main__":
    main()
