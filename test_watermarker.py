import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import shutil
import json
import zipfile
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

    # --- Watermark setting tests ---

    @patch('watermarker.requests.get')
    @patch('watermarker._is_safe_url', return_value=True)
    def test_set_watermark_success(self, mock_safe, mock_get):
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
    @patch('watermarker._is_safe_url', return_value=True)
    def test_set_watermark_failure(self, mock_safe, mock_get):
        mock_get.side_effect = Exception("Network error")
        result = watermarker.set_watermark(123, "http://bad.url")
        self.assertFalse(result)

    # --- SSRF protection tests ---

    @patch('socket.getaddrinfo')
    def test_is_safe_url_blocks_private_ip(self, mock_dns):
        mock_dns.return_value = [(None, None, None, None, ('192.168.1.1', 0))]
        self.assertFalse(watermarker._is_safe_url("http://internal.server/img.png"))

    @patch('socket.getaddrinfo')
    def test_is_safe_url_blocks_loopback(self, mock_dns):
        mock_dns.return_value = [(None, None, None, None, ('127.0.0.1', 0))]
        self.assertFalse(watermarker._is_safe_url("http://localhost/img.png"))

    @patch('socket.getaddrinfo')
    def test_is_safe_url_allows_public_ip(self, mock_dns):
        mock_dns.return_value = [(None, None, None, None, ('93.184.216.34', 0))]
        self.assertTrue(watermarker._is_safe_url("http://example.com/img.png"))

    def test_is_safe_url_blocks_non_http(self):
        self.assertFalse(watermarker._is_safe_url("file:///etc/passwd"))
        self.assertFalse(watermarker._is_safe_url("ftp://server/file"))

    def test_is_safe_url_blocks_empty(self):
        self.assertFalse(watermarker._is_safe_url(""))
        self.assertFalse(watermarker._is_safe_url("not-a-url"))

    @patch('watermarker._is_safe_url', return_value=False)
    def test_set_watermark_blocks_unsafe_url(self, mock_safe):
        result = watermarker.set_watermark(123, "http://192.168.1.1/evil.png")
        self.assertFalse(result)

    # --- Settings tests ---

    def test_settings_persistence(self):
        chat_id = "test_chat"
        defaults = watermarker.load_settings(chat_id)
        self.assertEqual(defaults["position"], "repeated")

        new_settings = {"position": "top left", "size": 0.5}
        watermarker.save_settings(chat_id, new_settings)

        loaded = watermarker.load_settings(chat_id)
        self.assertEqual(loaded["position"], "top left")
        self.assertEqual(loaded["size"], 0.5)

    def test_reset_settings(self):
        chat_id = "reset_chat"
        settings_path = watermarker.get_settings_path(chat_id)
        with open(settings_path, 'w') as f:
            f.write('{"test": true}')
        self.assertTrue(os.path.exists(settings_path))

        watermarker._reset_settings(chat_id)
        self.assertFalse(os.path.exists(settings_path))
        defaults = watermarker.load_settings(chat_id)
        self.assertEqual(defaults["position"], "repeated")

    def test_default_settings_values(self):
        defaults = watermarker.load_settings("nonexistent_chat")
        self.assertEqual(defaults["position"], "repeated")
        self.assertEqual(defaults["size"], 2.0)
        self.assertEqual(defaults["angle"], 45)
        self.assertEqual(defaults["mode"], "negate")
        self.assertEqual(defaults["strength"], 0.2)
        self.assertTrue(defaults["resize_8mp"])

    # --- Core watermark application tests ---

    def test_apply_watermark_default(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base.png")
        base_img = Image.new('RGB', (1000, 1000), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "test_watermark.png")
        watermark_img = Image.new('RGBA', (200, 200), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="top left", size=0.5)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(output_path))

        out_img = Image.open(output_path).convert("RGBA")

        pixel_in_watermark = out_img.getpixel((50, 50))
        self.assertEqual(pixel_in_watermark, (0, 0, 255, 255))

        pixel_outside_watermark = out_img.getpixel((10, 10))
        self.assertEqual(pixel_outside_watermark, (255, 255, 255, 255))

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
        base_path = os.path.join(watermarker.TEMP_DIR, "base_strength.png")
        base_img = Image.new('RGBA', (100, 100), color=(255, 255, 255, 255))
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black.png")
        watermark_img = Image.new('RGBA', (50, 50), color=(0, 0, 0, 255))
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_strength.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=0.5, strength=0.5)
        self.assertTrue(result)

        out_img = Image.open(output_path).convert("RGBA")
        center_pixel = out_img.getpixel((50, 50))

        self.assertTrue(100 < center_pixel[0] < 160, f"Pixel value {center_pixel} is not blending correctly")

    def test_apply_watermark_repeated_offset(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_offset.png")
        base_img = Image.new('RGB', (200, 200), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black_tile.png")
        watermark_img = Image.new('RGBA', (50, 50), color='black')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_offset.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="repeated", size=1.0)

        self.assertTrue(result)
        out_img = Image.open(output_path).convert("RGBA")

        pixel_in_offset_tile = out_img.getpixel((30, 60))
        self.assertEqual(pixel_in_offset_tile, (0, 0, 0, 255))

        pixel_in_gap = out_img.getpixel((79, 60))
        self.assertEqual(pixel_in_gap, (255, 255, 255, 255))

    def test_apply_watermark_resize(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_large.png")
        base_img = Image.new('RGB', (4000, 3000), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm.png")
        watermark_img = Image.new('RGBA', (50, 50), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_resized.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, max_pixels=8000000)
        self.assertTrue(result)

        out_img = Image.open(output_path)
        self.assertLessEqual(out_img.width * out_img.height, 8100000)  # small tolerance

    def test_apply_watermark_no_resize_when_small(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_small.png")
        base_img = Image.new('RGB', (100, 100), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm2.png")
        watermark_img = Image.new('RGBA', (10, 10), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_noresize.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, max_pixels=8000000)
        self.assertTrue(result)

        out_img = Image.open(output_path)
        self.assertEqual(out_img.width, 100)
        self.assertEqual(out_img.height, 100)

    def test_apply_watermark_difference_mode(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_diff.png")
        base_img = Image.new('RGB', (200, 200), color=(100, 150, 200))
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm_diff.png")
        watermark_img = Image.new('RGBA', (50, 50), color=(50, 50, 50, 255))
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_diff.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=1.0, mode="difference")
        self.assertTrue(result)

        out_img = Image.open(output_path).convert("RGB")
        center_pixel = out_img.getpixel((100, 100))
        # difference: |100-50|=50, |150-50|=100, |200-50|=150
        self.assertEqual(center_pixel, (50, 100, 150))

    def test_apply_watermark_negate_mode(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_neg.png")
        base_img = Image.new('RGB', (200, 200), color=(100, 150, 200))
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm_neg.png")
        watermark_img = Image.new('RGBA', (50, 50), color=(255, 255, 255, 255))
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_neg.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=1.0, mode="negate")
        self.assertTrue(result)

        out_img = Image.open(output_path).convert("RGB")
        center_pixel = out_img.getpixel((100, 100))
        # negate: 255-100=155, 255-150=105, 255-200=55
        self.assertEqual(center_pixel, (155, 105, 55))

    def test_apply_watermark_angle(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_angle.png")
        base_img = Image.new('RGB', (500, 500), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm_angle.png")
        watermark_img = Image.new('RGBA', (100, 100), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output_angle.png")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=1.0, angle=45)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(output_path))

    def test_apply_watermark_jpg_output(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_jpg.png")
        base_img = Image.new('RGB', (200, 200), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm_jpg.png")
        watermark_img = Image.new('RGBA', (50, 50), color='blue')
        watermark_img.save(watermark_path)

        output_path = os.path.join(watermarker.TEMP_DIR, "output.jpg")

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="center", size=1.0)
        self.assertTrue(result)

        out_img = Image.open(output_path)
        self.assertEqual(out_img.mode, "RGB")

    def test_apply_watermark_all_positions(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_pos.png")
        base_img = Image.new('RGB', (500, 500), color='white')
        base_img.save(base_path)

        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "wm_pos.png")
        watermark_img = Image.new('RGBA', (50, 50), color='blue')
        watermark_img.save(watermark_path)

        positions = ['top left', 'top', 'top right', 'left', 'center', 'right',
                     'bottom left', 'bottom', 'bottom right']

        for pos in positions:
            output_path = os.path.join(watermarker.TEMP_DIR, f"output_{pos.replace(' ', '_')}.png")
            result = watermarker.apply_watermark(base_path, watermark_path, output_path, position=pos, size=1.0)
            self.assertTrue(result, f"Failed for position: {pos}")
            self.assertTrue(os.path.exists(output_path), f"Output missing for position: {pos}")

    def test_apply_watermark_invalid_path(self):
        result = watermarker.apply_watermark("/nonexistent/path.png", "/nonexistent/wm.png", "/tmp/out.png")
        self.assertFalse(result)

    # --- Bot command tests ---

    @patch('watermarker.tele.send_telegram')
    def test_process_text_commands(self, mock_send):
        chat_id = 123
        watermarker._reset_settings(chat_id)
        mock_send.reset_mock()

        # Test Start
        with patch('watermarker._reset_settings') as mock_reset:
            watermarker.process_text("token", chat_id, "/start")
            mock_reset.assert_called_with(chat_id)
            self.assertIn("Welcome to Watermarker Bot!", mock_send.call_args[0][2])
            self.assertIn("GitHub Repo: https://github.com/puchkarev/watermarker", mock_send.call_args[0][2])
        mock_send.reset_mock()

        # Test Settings
        watermarker.process_text("token", chat_id, "/settings")
        last_call_args = mock_send.call_args_list[-1][0][2]
        self.assertIn("Current Settings:", last_call_args)
        self.assertIn("- position: repeated", last_call_args)
        self.assertIn("- size: 2.0", last_call_args)
        self.assertIn("- angle: 45", last_call_args)
        self.assertIn("- mode: negate", last_call_args)
        self.assertIn("- strength: 0.2", last_call_args)
        self.assertIn("- resize_8mp: True", last_call_args)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_help(self, mock_send):
        watermarker.process_text("token", 123, "/help")
        msg = mock_send.call_args[0][2]
        self.assertIn("Welcome to Watermarker Bot!", msg)
        self.assertIn("/source", msg)
        self.assertIn("0.1 for 10%", msg)  # verify typo is fixed

    @patch('watermarker.tele.send_telegram')
    def test_process_text_position(self, mock_send):
        chat_id = 456
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/position top left")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["position"], "top left")

    @patch('watermarker.tele.send_telegram')
    def test_process_text_position_invalid(self, mock_send):
        watermarker.process_text("token", 456, "/position invalid_pos")
        msg = mock_send.call_args[0][2]
        self.assertIn("Invalid position", msg)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_size(self, mock_send):
        chat_id = 789
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/size 0.5")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["size"], 0.5)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_size_allows_above_1(self, mock_send):
        chat_id = 790
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/size 2.0")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["size"], 2.0)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_size_rejects_above_10(self, mock_send):
        chat_id = 791
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/size 15.0")
        msg = mock_send.call_args[0][2]
        self.assertIn("Size must be between 0.0 and 10.0", msg)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_size_invalid(self, mock_send):
        watermarker.process_text("token", 789, "/size notanumber")
        msg = mock_send.call_args[0][2]
        self.assertIn("Invalid number format", msg)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_strength(self, mock_send):
        chat_id = 800
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/strength 0.7")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["strength"], 0.7)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_strength_out_of_range(self, mock_send):
        watermarker.process_text("token", 800, "/strength 1.5")
        msg = mock_send.call_args[0][2]
        self.assertIn("Strength must be between 0.0 and 1.0", msg)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_angle(self, mock_send):
        chat_id = 810
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/angle 90")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["angle"], 90.0)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_mode(self, mock_send):
        chat_id = 820
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/mode difference")
        settings = watermarker.load_settings(chat_id)
        self.assertEqual(settings["mode"], "difference")

    @patch('watermarker.tele.send_telegram')
    def test_process_text_mode_invalid(self, mock_send):
        watermarker.process_text("token", 820, "/mode invalid")
        msg = mock_send.call_args[0][2]
        self.assertIn("Invalid mode", msg)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_resize_8mp(self, mock_send):
        chat_id = 830
        watermarker._reset_settings(chat_id)

        watermarker.process_text("token", chat_id, "/resize_8mp false")
        settings = watermarker.load_settings(chat_id)
        self.assertFalse(settings["resize_8mp"])

        watermarker.process_text("token", chat_id, "/resize_8mp true")
        settings = watermarker.load_settings(chat_id)
        self.assertTrue(settings["resize_8mp"])

    @patch('watermarker.tele.send_telegram')
    def test_process_text_source_no_url(self, mock_send):
        watermarker.process_text("token", 123, "/source")
        # Should not crash; no message sent for missing url (current behavior)

    # --- Photo processing tests ---

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker.tele.get_telegram_file')
    @patch('watermarker.apply_watermark')
    def test_process_photo(self, mock_apply, mock_get_file, mock_send_file):
        watermark_path = watermarker.get_watermark_path(123)
        with open(watermark_path, 'w') as f:
            f.write("dummy")

        mock_get_file.return_value = "photo.jpg"
        mock_apply.return_value = True

        with open(os.path.join(watermarker.TEMP_DIR, "photo.jpg"), 'w') as f:
            f.write("dummy content")

        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 123, photo_list)

        mock_get_file.assert_called()
        mock_apply.assert_called()

        self.assertIn("position", mock_apply.call_args[1])
        self.assertIn("size", mock_apply.call_args[1])
        self.assertIn("strength", mock_apply.call_args[1])
        self.assertIn("angle", mock_apply.call_args[1])
        self.assertIn("mode", mock_apply.call_args[1])
        self.assertIn("max_pixels", mock_apply.call_args[1])

        mock_send_file.assert_called()

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.os.path.exists', return_value=False)
    def test_process_photo_no_watermark(self, mock_exists, mock_send):
        # No watermark file and no default sun.webp
        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 99999, photo_list)
        msg = mock_send.call_args[0][2]
        self.assertIn("No watermark set", msg)

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker.tele.get_telegram_file')
    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.apply_watermark')
    def test_process_photo_apply_fails(self, mock_apply, mock_send, mock_get_file, mock_send_file):
        watermark_path = watermarker.get_watermark_path(555)
        with open(watermark_path, 'w') as f:
            f.write("dummy")

        mock_get_file.return_value = "photo.jpg"
        mock_apply.return_value = False

        with open(os.path.join(watermarker.TEMP_DIR, "photo.jpg"), 'w') as f:
            f.write("dummy content")

        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 555, photo_list)

        mock_send.assert_called()
        msg = mock_send.call_args[0][2]
        self.assertIn("Error processing image", msg)

    # --- Document/zip processing tests ---

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker.tele.get_telegram_file')
    @patch('watermarker.apply_watermark')
    @patch('zipfile.ZipFile')
    @patch('shutil.make_archive')
    def test_process_document_zip(self, mock_make_archive, mock_zipfile, mock_apply, mock_get_file, mock_send_file):
        chat_id = 123
        doc = {"file_id": "zip_id", "file_name": "images.zip", "mime_type": "application/zip"}

        mock_get_file.return_value = "images.zip"

        def extract_side_effect(path):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "test.jpg"), 'w') as f:
                f.write("dummy")

        mock_zip_instance = MagicMock()
        mock_zip_instance.extractall.side_effect = extract_side_effect
        mock_zip_instance.namelist.return_value = ["test.jpg"]
        mock_zipfile.return_value.__enter__.return_value = mock_zip_instance

        mock_apply.return_value = True

        watermarker.process_document("token", chat_id, doc)

        mock_get_file.assert_called()
        mock_zipfile.assert_called()
        mock_apply.assert_called()
        mock_make_archive.assert_called()
        mock_send_file.assert_called()

    # --- Zip-slip protection test ---

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker.tele.get_telegram_file')
    @patch('watermarker.tele.send_telegram')
    def test_process_document_zip_slip_blocked(self, mock_send, mock_get_file, mock_send_file):
        chat_id = 999
        doc = {"file_id": "zip_id", "file_name": "evil.zip", "mime_type": "application/zip"}

        # Create a real zip file with a path traversal entry
        zip_path = os.path.join(watermarker.TEMP_DIR, "evil.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("../../etc/passwd", "evil content")

        mock_get_file.return_value = "evil.zip"

        watermarker.process_document("token", chat_id, doc)

        # Should report error, not extract
        mock_send.assert_called()
        msg = mock_send.call_args[0][2]
        self.assertIn("Error processing zip", msg)

    # --- handle_update tests ---

    @patch('watermarker.process_text')
    def test_handle_update_text(self, mock_process):
        update = {"message": {"chat": {"id": 1}, "text": "/help"}}
        watermarker.handle_update("token", update)
        mock_process.assert_called_with("token", 1, "/help")

    @patch('watermarker.process_photo')
    def test_handle_update_photo(self, mock_process):
        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        update = {"message": {"chat": {"id": 1}, "photo": photo_list}}
        watermarker.handle_update("token", update)
        mock_process.assert_called_with("token", 1, photo_list)

    @patch('watermarker.process_document')
    def test_handle_update_document(self, mock_process):
        doc = {"file_id": "fid", "file_name": "test.zip", "mime_type": "application/zip"}
        update = {"message": {"chat": {"id": 1}, "document": doc}}
        watermarker.handle_update("token", update)
        mock_process.assert_called_with("token", 1, doc)

    def test_handle_update_no_message(self):
        # Should not crash
        watermarker.handle_update("token", {"update_id": 1})

if __name__ == '__main__':
    unittest.main()
