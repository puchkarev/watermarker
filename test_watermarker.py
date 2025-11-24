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
        # Create base image
        base_path = os.path.join(watermarker.TEMP_DIR, "base.png")
        base_img = Image.new('RGB', (800, 600), color='white')
        base_img.save(base_path)

        # Create watermark image
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "123.png")
        watermark_img = Image.new('RGBA', (200, 200), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output.png")
        settings = {"position": "bottom right", "size": 0.25}
        
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, settings)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(output_path))
        
        out_img = Image.open(output_path)
        self.assertEqual(out_img.size, (800, 600))

    def test_apply_watermark_repeated(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_tiled.png")
        base_img = Image.new('RGB', (500, 500), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "tiler.png")
        watermark_img = Image.new('RGBA', (50, 50), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_tiled.png")
        settings = {"position": "repeated", "size": 0.1} # 50px width
        
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, settings)
        self.assertTrue(result)

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
        # Verify apply was called with settings (dict) as last arg
        self.assertIsInstance(mock_apply.call_args[0][3], dict)
        mock_send_file.assert_called()

if __name__ == '__main__':
    unittest.main()