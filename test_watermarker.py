import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import shutil
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

    def test_apply_watermark(self):
        # Create base image
        base_path = os.path.join(watermarker.TEMP_DIR, "base.png")
        base_img = Image.new('RGB', (800, 600), color='white')
        base_img.save(base_path)

        # Create watermark image
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "123.png")
        watermark_img = Image.new('RGBA', (200, 200), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output.png")
        
        result = watermarker.apply_watermark(base_path, watermark_path, output_path)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(output_path))
        
        # Verify result dimensions
        out_img = Image.open(output_path)
        self.assertEqual(out_img.size, (800, 600))
        # We can't easily check pixels without precise coordinates, but we know it shouldn't crash

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.set_watermark')
    def test_process_text_source(self, mock_set_watermark, mock_send):
        mock_set_watermark.return_value = True
        watermarker.process_text("token", 123, "/source http://img.com")
        
        mock_set_watermark.assert_called_with(123, "http://img.com")
        mock_send.assert_called()
        self.assertIn("successfully", mock_send.call_args[0][2])

    @patch('watermarker.tele.send_telegram')
    def test_process_text_invalid(self, mock_send):
        watermarker.process_text("token", 123, "/source")
        mock_send.assert_called()
        self.assertIn("Usage", mock_send.call_args[0][2])

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
        mock_send_file.assert_called()

if __name__ == '__main__':
    unittest.main()
