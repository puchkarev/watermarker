import argparse
import sys
import os
from PIL import Image, ImageChops, ImageOps

try:
    # Lets Pillow open iPhone HEIC/HEIF photos when pillow-heif is installed
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    HEIF_SUPPORTED = False

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
if HEIF_SUPPORTED:
    SUPPORTED_EXTS |= {".heic", ".heif"}

DEFAULT_QUALITY = 80

def apply_watermark(base_image_path, watermark_path="sun.webp", output_path="output.webp", *,
                    position="bottom right", size=0.25, max_pixels=None, mode="standard", angle=0,
                    strength=1.0, x_offset=1.0, y_offset=1.0, quality=DEFAULT_QUALITY):
    """
    Applies a watermark to an image.

    Everything after the three paths is keyword-only: several options are bare
    numbers, and passing them positionally would let a swap go unnoticed.
    
    Args:
        base_image_path (str): Path to the base image.
        watermark_path (str): Path to the watermark image.
        output_path (str): Path to save the result.
        position (str): Position of the watermark. 
                        Options: 'top left', 'top', 'top right', 'left', 'center', 'right', 
                                 'bottom left', 'bottom', 'bottom right', 'repeated'.
        size (float): Size of the watermark as a fraction of the base image width (0.0 - 1.0).
        max_pixels (int): Maximum number of pixels for the output image. If input is larger, it will be resized.
        mode (str): Watermarking mode. 'standard', 'difference', or 'negate'.
        angle (float): Rotation angle of the watermark in degrees.
        strength (float): Strength/Opacity of the watermark (0.0 - 1.0).
        x_offset (float): Horizontal spacing as a multiple of the watermark width
            (1.0 = no gap between tiles / flush with the edge).
        y_offset (float): Vertical spacing as a multiple of the watermark height.
        quality (int): Encoder quality for JPEG/WebP output (1 - 100).
    
    Returns:
        bool: True if successful, False otherwise.
    """
    try:
        with Image.open(base_image_path) as source:
            # Keep an RGB colour profile (e.g. Adobe RGB, Display P3) so colours don't shift.
            # Gray/CMYK profiles no longer describe the pixels once converted to RGB, so
            # those are dropped (untagged RGB is read as sRGB). Bytes 16-20 of an ICC
            # header name the profile's colour space.
            icc_profile = source.info.get("icc_profile")
            if icc_profile and icc_profile[16:20] != b"RGB ":
                icc_profile = None
            # Cameras and phones often store portrait shots sideways plus a rotate tag;
            # apply the rotation, since the tag isn't carried into the output
            try:
                upright = ImageOps.exif_transpose(source)
            except Exception as e:
                # A damaged EXIF block shouldn't stop the photo being watermarked
                print(f"Ignoring unreadable EXIF orientation in {base_image_path}: {e}")
                upright = source
            base = upright.convert("RGBA")
        
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

        # Calculate watermark size based on its original dimensions
        original_watermark_width, original_watermark_height = watermark.size
        target_width = int(original_watermark_width * size)
        if target_width < 1: target_width = 1
        aspect_ratio = original_watermark_height / original_watermark_width
        target_height = int(target_width * aspect_ratio)
        if target_height < 1: target_height = 1
        
        watermark_resized = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)
        
        # Rotate if needed
        if angle != 0:
            watermark_resized = watermark_resized.rotate(angle, expand=True, resample=Image.BICUBIC)
            target_width, target_height = watermark_resized.size

        # Apply strength (opacity)
        if strength < 1.0:
            strength = max(0.0, strength)
            r, g, b, a = watermark_resized.split()
            a = a.point(lambda p: int(p * strength))
            watermark_resized = Image.merge("RGBA", (r, g, b, a))

        # Create a transparent layer for the watermark content
        # For 'standard', we paste the watermark here.
        # For others, we might need a mask.
        
        watermark_layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
        
        x_padding = int(target_width * (x_offset - 1.0))
        y_padding = int(target_height * (y_offset - 1.0))
        
        positions = []
        if position == "repeated":
            x_step = target_width + x_padding
            y_step = target_height + y_padding
            if x_step < 1:
                raise ValueError(f"x_offset {x_offset} is too small: tiles would not advance horizontally")
            if y_step < 1:
                raise ValueError(f"y_offset {y_offset} is too small: tiles would not advance vertically")

            # Tile the watermark with brick offset
            row_index = 0
            for y in range(0, base.height, y_step):
                offset = 0
                if row_index % 2 == 1:
                    offset = x_step // 2
                
                # Start x from -offset to ensure coverage on the left
                for x in range(-offset, base.width, x_step):
                    positions.append((x, y))
                row_index += 1
        else:
            # Calculate coordinates
            # Default to bottom right
            x = base.width - target_width - x_padding
            y = base.height - target_height - y_padding
            
            if position == "top left":
                x = x_padding
                y = y_padding
            elif position == "top":
                x = (base.width - target_width) // 2
                y = y_padding
            elif position == "top right":
                x = base.width - target_width - x_padding
                y = y_padding
            elif position == "left":
                x = x_padding
                y = (base.height - target_height) // 2
            elif position == "center":
                x = (base.width - target_width) // 2
                y = (base.height - target_height) // 2
            elif position == "right":
                x = base.width - target_width - x_padding
                y = (base.height - target_height) // 2
            elif position == "bottom left":
                x = x_padding
                y = base.height - target_height - y_padding
            elif position == "bottom":
                x = (base.width - target_width) // 2
                y = base.height - target_height - y_padding
            elif position == "bottom right":
                x = base.width - target_width - x_padding
                y = base.height - target_height - y_padding
            
            positions.append((x, y))

        # Paste watermark onto the layer
        for pos in positions:
            watermark_layer.paste(watermark_resized, pos)

        # Apply based on mode
        if mode == "standard":
            # Composite
            result = Image.alpha_composite(base, watermark_layer)
        
        elif mode == "difference":
            # Difference mode: |Base - Watermark|
            # We only apply this where the watermark exists.
            
            # Extract RGB channels
            base_rgb = base.convert("RGB")
            wm_rgb = watermark_layer.convert("RGB")
            wm_a = watermark_layer.split()[3]
            
            # Calculate difference
            diff_rgb = ImageChops.difference(base_rgb, wm_rgb)
            
            # Blend diff and base using watermark alpha
            # If alpha is 255, we see diff. If 0, we see base.
            result_rgb = Image.composite(diff_rgb, base_rgb, wm_a)
            result = result_rgb.convert("RGBA")

        elif mode == "negate":
            # Invert mode: Invert base image where watermark is opaque
            # Result = Base * (1-Alpha) + (1-Base) * Alpha  (conceptually)
            
            base_rgb = base.convert("RGB")
            wm_a = watermark_layer.split()[3]
            
            inverted_base = ImageChops.invert(base_rgb)
            
            # Composite inverted base and original base using mask
            result_rgb = Image.composite(inverted_base, base_rgb, wm_a)
            result = result_rgb.convert("RGBA")
            
        else:
            # Fallback to standard
            result = Image.alpha_composite(base, watermark_layer)
        
        # Save logic
        # If output path implies a specific format, use it.
        # But if we want to ensure webp in batch mode, the extension is already set in output_path.
        
        save_options = {}
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        if output_path.lower().endswith((".jpg", ".jpeg", ".webp")):
            save_options["quality"] = quality
        if output_path.lower().endswith((".jpg", ".jpeg")):
            result = result.convert("RGB")

        result.save(output_path, **save_options)
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
    parser.add_argument("--mode", default="standard", choices=["standard", "difference", "negate"],
                        help="Watermark blending mode. 'standard' (overlay), 'difference' (color diff), or 'negate' (inversion).")
    parser.add_argument("--angle", type=float, default=0, help="Rotation angle of the watermark in degrees.")
    parser.add_argument("--strength", type=float, default=1.0, help="Watermark strength/opacity (0.0 - 1.0).")
    parser.add_argument("--x-offset", type=float, default=1.0, help="Horizontal spacing as a multiple of the watermark width.")
    parser.add_argument("--y-offset", type=float, default=1.0, help="Vertical spacing as a multiple of the watermark height.")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="JPEG/WebP output quality (1 - 100).")

    args = parser.parse_args()
    
    max_pixels = 8000000 if args.resize_8mp else None
    options = dict(position=args.position, size=args.size, max_pixels=max_pixels, mode=args.mode,
                   angle=args.angle, strength=args.strength, x_offset=args.x_offset,
                   y_offset=args.y_offset, quality=args.quality)

    if os.path.isdir(args.base_image):
        # Batch processing
        if not os.path.exists(args.output_image):
            try:
                os.makedirs(args.output_image)
            except OSError as e:
                print(f"Error creating output directory: {e}")
                sys.exit(1)
                
        processed_count = 0
        
        for filename in os.listdir(args.base_image):
            ext = os.path.splitext(filename)[1].lower()
            if ext in SUPPORTED_EXTS:
                input_path = os.path.join(args.base_image, filename)
                # Feature: Change encoding to webp for batch processing
                output_filename = os.path.splitext(filename)[0] + ".webp"
                output_path = os.path.join(args.output_image, output_filename)
                
                print(f"Processing {filename} -> {output_filename}...")
                if apply_watermark(input_path, args.watermark_image, output_path, **options):
                    processed_count += 1
                else:
                    print(f"Failed to process {filename}")
        
        print(f"Batch processing complete. {processed_count} images processed.")
        sys.exit(0)

    else:
        # Single file processing
        if apply_watermark(args.base_image, args.watermark_image, args.output_image, **options):
            print(f"Successfully saved to {args.output_image}")
            sys.exit(0)
        else:
            print("Failed to apply watermark")
            sys.exit(1)

if __name__ == "__main__":
    main()
