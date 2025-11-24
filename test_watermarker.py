import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import shutil
import json
from PIL import Image
from io import BytesIO

# Ensure we can import watermarker
sys.path.append(os.path.dirname(__file__))
import watermarker

class TestWatermarker(unittest.TestCase):

    def setUp(self):
        self.test_dir = "test_data"
        if not os.path.exists(self.test_dir):
            os.makedirs(self.test_dir)
        watermarker.WATERMARKS_DIR = os.path.join(self.test_dir, "watermarks")
        watermarker.TEMP_DIR = os.path.join(self.test_dir, "temp")
        watermarker.SETTINGS_DIR = os.path.join(self.test_dir, "settings")
        watermarker.ensure_dirs()

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    @patch('watermarker.requests.get')
    def test_set_watermark_success(self, mock_get):
        # Create a dummy image
        img = Image.new('RGB', (100, 100), color='red')
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)

        mock_response = MagicMock()
        mock_response.content = img_byte_arr.read()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        chat_id = 12345
        url = "http://example.com/image.png"
        
        result = watermarker.set_watermark(chat_id, url)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(watermarker.get_watermark_path(chat_id)))

    @patch('watermarker.requests.get')
    def test_set_watermark_failure(self, mock_get):
        mock_get.side_effect = Exception("Network error")
        result = watermarker.set_watermark(123, "http://bad.url")
        self.assertFalse(result)

    def test_settings_persistence(self):
        chat_id = "test_chat"
        defaults = watermarker.load_settings(chat_id)
        self.assertEqual(defaults["position"], "bottom right")
        
        new_settings = {"position": "top left", "size": 0.5}
        watermarker.save_settings(chat_id, new_settings)
        
        loaded = watermarker.load_settings(chat_id)
        self.assertEqual(loaded["position"], "top left")
        self.assertEqual(loaded["size"], 0.5)

    def test_apply_watermark_default(self):
        # Create base image (large enough for various watermark sizes)
        base_path = os.path.join(watermarker.TEMP_DIR, "base.png")
        base_img = Image.new('RGB', (1000, 1000), color='white')
        base_img.save(base_path)

        # Create watermark image (e.g., 200x200 solid blue)
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "test_watermark.png")
        watermark_img = Image.new('RGBA', (200, 200), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output.png")
        
        # Apply watermark with size 0.5 (should make watermark 100x100)
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="top left", size=0.5)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(output_path))
        
        out_img = Image.open(output_path).convert("RGBA")
        
        # Verify the watermark is present and scaled correctly.
        # Original watermark 200x200. Size 0.5 means it should be 100x100.
        # Positioned at top-left, with 2% padding (1000 * 0.02 = 20 pixels).
        # So, watermark should be from (20,20) to (120,120).
        # Check a pixel within the expected watermark area (e.g., 50,50)
        # and a pixel outside (e.g., 10,10, which should be white).
        
        # Pixel inside watermark area, should be blue
        pixel_in_watermark = out_img.getpixel((50, 50))
        self.assertEqual(pixel_in_watermark, (0, 0, 255, 255))
        
        # Pixel outside watermark area (padding), should be white
        pixel_outside_watermark = out_img.getpixel((10, 10))
        self.assertEqual(pixel_outside_watermark, (255, 255, 255, 255))

        # Pixel just outside the 100x100 watermark, to ensure it's not larger
        pixel_right_of_watermark = out_img.getpixel((125, 50))
        self.assertEqual(pixel_right_of_watermark, (255, 255, 255, 255))
        pixel_below_watermark = out_img.getpixel((50, 125))
        self.assertEqual(pixel_below_watermark, (255, 255, 255, 255))

    def test_apply_watermark_repeated(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_tiled.png")
        base_img = Image.new('RGB', (500, 500), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "tiler.png")
        watermark_img = Image.new('RGBA', (50, 50), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_tiled.png")
        
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="repeated", size=0.1)
        self.assertTrue(result)

    def test_apply_watermark_strength(self):
        # Base: White
        base_path = os.path.join(watermarker.TEMP_DIR, "base_strength.png")
        base_img = Image.new('RGBA', (100, 100), color=(255, 255, 255, 255))
        base_img.save(base_path)

        # Watermark: Black
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black.png")
        watermark_img = Image.new('RGBA', (50, 50), color=(0, 0, 0, 255))
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_strength.png")
        
        # Apply with 50% strength at "center"
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=0.5, strength=0.5)
        self.assertTrue(result)
        
        # Check pixel color at center
        out_img = Image.open(output_path).convert("RGBA")
        center_pixel = out_img.getpixel((50, 50))
        
        # Should be grey (around 127) not black (0) and not white (255)
        # 255 * 0.5 = ~127
        self.assertTrue(100 < center_pixel[0] < 160, f"Pixel value {center_pixel} is not blending correctly")

    def test_apply_watermark_repeated_offset(self):
        # Base: 200x200 White
        base_path = os.path.join(watermarker.TEMP_DIR, "base_offset.png")
        base_img = Image.new('RGB', (200, 200), color='white')
        base_img.save(base_path)

        # Watermark: 50x50 Black
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black_tile.png")
        watermark_img = Image.new('RGBA', (50, 50), color='black')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_offset.png")
        
        # Apply repeated. Size 0.25 -> 50x50 pixels. Padding ~4px.
        # Row 0: x=0, x=54, ...
        # Row 1: y=54. Offset = (50+4)//2 = 27. x=-27, x=27, x=81...
        # So at (27, 54) we should see the start of a tile (black).
        
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="repeated", size=1.0) 
        # size=1.0 relative to original watermark means 50x50.
        
        self.assertTrue(result)
        out_img = Image.open(output_path).convert("RGBA")
        
        # Check a pixel that should be black due to offset in the second row
        # Row 0 height is 50 + padding. Padding = 200*0.02 = 4. 
        # Row 0 is y=0 to 50. Row 1 starts at y=54.
        # Row 1 offset is (50+4)//2 = 27.
        # Tile starts at x=27.
        # So (30, 60) should be black.
        
        # NOTE: Padding calculation is int(base.width * 0.02) = 4.
        
        pixel_in_offset_tile = out_img.getpixel((30, 60))
        self.assertEqual(pixel_in_offset_tile, (0, 0, 0, 255))
        
        # Check a pixel that would have been black if NOT offset, but is now white (gap or before tile)
        # If no offset, tile starts at x=0. x=10 would be black.
        # With offset 27, x=0 to 27 should be white (padding/gap from previous tile starting at -27).
        # Previous tile: starts -27. Ends -27+50 = 23. 
        # So x=10 IS inside the tile from negative start.
        
        # Let's check the gap between tiles in Row 1.
        # Tile 1: 27 to 77.
        # Tile 2: 27+54 = 81 to 131.
        # Gap between 77 and 81.
        # Pixel at (79, 60) should be white.
        pixel_in_gap = out_img.getpixel((79, 60))
        self.assertEqual(pixel_in_gap, (255, 255, 255, 255))

    @patch('watermarker.tele.send_telegram')
    def test_process_text_commands(self, mock_send):
        chat_id = 123
        
        # Test Help
        watermarker.process_text("token", chat_id, "/help")
        mock_send.assert_called()
        self.assertIn("Available commands", mock_send.call_args[0][2])
        
        # Test Position Success
        watermarker.process_text("token", chat_id, "/position top left")
        self.assertIn("Position set to: top left", mock_send.call_args[0][2])
        
        # Test Position Invalid
        watermarker.process_text("token", chat_id, "/position invalid")
        self.assertIn("Invalid position", mock_send.call_args[0][2])
        
        # Test Size Success
        watermarker.process_text("token", chat_id, "/size 0.5")
        self.assertIn("Size set to: 0.5", mock_send.call_args[0][2])
        
        # Test Size Invalid
        watermarker.process_text("token", chat_id, "/size 1.5")
        self.assertIn("Size must be between", mock_send.call_args[0][2])

        # Test Strength Success
        watermarker.process_text("token", chat_id, "/strength 0.8")
        self.assertIn("Strength set to: 0.8", mock_send.call_args[0][2])

        # Test Strength Invalid Value
        watermarker.process_text("token", chat_id, "/strength 1.5")
        self.assertIn("Strength must be between", mock_send.call_args[0][2])
        
        # Test Strength Invalid Format
        watermarker.process_text("token", chat_id, "/strength abc")
        self.assertIn("Invalid number format", mock_send.call_args[0][2])

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker.tele.get_telegram_file')
    @patch('watermarker.apply_watermark')
    def test_process_photo(self, mock_apply, mock_get_file, mock_send_file):
        # Setup watermark
        watermark_path = watermarker.get_watermark_path(123)
        with open(watermark_path, 'w') as f:
            f.write("dummy")

        mock_get_file.return_value = "photo.jpg"
        mock_apply.return_value = True
        
        # Create dummy downloaded file
        with open(os.path.join(watermarker.TEMP_DIR, "photo.jpg"), 'w') as f:
            f.write("dummy content")

        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 123, photo_list)
        
        mock_get_file.assert_called()
        mock_apply.assert_called()
        
        # Verify kwargs are used
        self.assertIn("position", mock_apply.call_args[1])
        self.assertIn("size", mock_apply.call_args[1])
        self.assertIn("strength", mock_apply.call_args[1])
        
        mock_send_file.assert_called()

if __name__ == '__main__':
    unittest.main()