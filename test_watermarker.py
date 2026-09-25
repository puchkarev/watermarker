import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import shutil
import json
import zipfile
import errno
import time
from PIL import Image
from io import BytesIO

# Ensure we can import watermarker
sys.path.append(os.path.dirname(__file__))
import watermarker
from watermarker_core import apply_watermark, HEIF_SUPPORTED, SUPPORTED_EXTS

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

        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="top left", size=0.5,
                                             x_offset=1.2, y_offset=1.2)
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

        # 50px tiles with a 4px gap
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="repeated", size=1.0,
                                             x_offset=1.08, y_offset=1.08)

        self.assertTrue(result)
        out_img = Image.open(output_path).convert("RGBA")

        pixel_in_offset_tile = out_img.getpixel((30, 60))
        self.assertEqual(pixel_in_offset_tile, (0, 0, 0, 255))

        pixel_in_gap = out_img.getpixel((79, 60))
        self.assertEqual(pixel_in_gap, (255, 255, 255, 255))

    def test_apply_watermark_repeated_spacing(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_spacing.png")
        Image.new('RGB', (200, 200), color='white').save(base_path)
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black_tile.png")
        Image.new('RGBA', (50, 50), color='black').save(watermark_path)
        output_path = os.path.join(watermarker.TEMP_DIR, "output_spacing.png")

        # Offset 3.0 leaves a gap of twice the tile size: tiles every 150px,
        # odd rows shifted by half a step (75px)
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="repeated", size=1.0,
                                             x_offset=3.0, y_offset=3.0)
        self.assertTrue(result)
        out_img = Image.open(output_path).convert("RGBA")

        black, white = (0, 0, 0, 255), (255, 255, 255, 255)
        self.assertEqual(out_img.getpixel((25, 25)), black)    # first tile
        self.assertEqual(out_img.getpixel((175, 25)), black)   # next tile in row
        self.assertEqual(out_img.getpixel((100, 25)), white)   # horizontal gap
        self.assertEqual(out_img.getpixel((25, 100)), white)   # vertical gap
        self.assertEqual(out_img.getpixel((100, 175)), black)  # shifted second row
        self.assertEqual(out_img.getpixel((25, 175)), white)

    def test_apply_watermark_position_offset(self):
        base_path = os.path.join(watermarker.TEMP_DIR, "base_pos.png")
        Image.new('RGB', (400, 400), color='white').save(base_path)
        watermark_path = os.path.join(watermarker.WATERMARKS_DIR, "black_tile.png")
        Image.new('RGBA', (50, 50), color='black').save(watermark_path)
        output_path = os.path.join(watermarker.TEMP_DIR, "output_pos.png")

        # Offset 3.0 puts the watermark two tile-widths in from the edge: 250..300
        result = watermarker.apply_watermark(base_path, watermark_path, output_path, position="bottom right", size=1.0,
                                             x_offset=3.0, y_offset=3.0)
        self.assertTrue(result)
        out_img = Image.open(output_path).convert("RGBA")
        self.assertEqual(out_img.getpixel((275, 275)), (0, 0, 0, 255))
        self.assertEqual(out_img.getpixel((375, 375)), (255, 255, 255, 255))

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
        self.assertIn("Usage: /source", mock_send.call_args[0][2])

    # --- Photo processing tests ---

    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    @patch('watermarker.apply_watermark')
    def test_process_photo(self, mock_apply, mock_get_file, mock_send_file):
        watermark_path = watermarker.get_watermark_path(123)
        with open(watermark_path, 'w') as f:
            f.write("dummy")

        mock_get_file.return_value = os.path.join(watermarker.TEMP_DIR, "photo.jpg")
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
        self.assertEqual(mock_apply.call_args[1]["x_offset"], 3.0)
        self.assertEqual(mock_apply.call_args[1]["y_offset"], 3.0)
        self.assertEqual(mock_apply.call_args[1]["quality"], 80)

        # Sent back as a file so Telegram doesn't recompress it
        mock_send_file.assert_called()
        self.assertEqual(mock_send_file.call_args[0][3], "watermarked.jpg")

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.os.path.exists', return_value=False)
    def test_process_photo_no_watermark(self, mock_exists, mock_send):
        # No watermark file and no default sun.webp
        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 99999, photo_list)
        msg = mock_send.call_args[0][2]
        self.assertIn("No watermark set", msg)

    @patch('watermarker.tele.send_telegram_file')
    @patch('watermarker._download_file')
    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.apply_watermark')
    def test_process_photo_apply_fails(self, mock_apply, mock_send, mock_get_file, mock_send_file):
        watermark_path = watermarker.get_watermark_path(555)
        with open(watermark_path, 'w') as f:
            f.write("dummy")

        mock_get_file.return_value = os.path.join(watermarker.TEMP_DIR, "photo.jpg")
        mock_apply.return_value = False

        with open(os.path.join(watermarker.TEMP_DIR, "photo.jpg"), 'w') as f:
            f.write("dummy content")

        photo_list = [{"file_id": "fid", "width": 100, "height": 100}]
        watermarker.process_photo("token", 555, photo_list)

        mock_send.assert_called()
        msg = mock_send.call_args[0][2]
        self.assertIn("Error processing image", msg)

    # --- Document/zip processing tests ---

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    @patch('watermarker.apply_watermark')
    @patch('zipfile.ZipFile')
    @patch('shutil.make_archive')
    def test_process_document_zip(self, mock_make_archive, mock_zipfile, mock_apply, mock_get_file, mock_send_file, mock_send):
        chat_id = 123
        doc = {"file_id": "zip_id", "file_name": "images.zip", "mime_type": "application/zip"}

        mock_get_file.return_value = os.path.join(watermarker.TEMP_DIR, "1234.zip")

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
        # Result is sent back under the original file name
        self.assertEqual(mock_send_file.call_args[0][3], "images.zip")

    # --- Zip-slip protection test ---

    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    @patch('watermarker.tele.send_telegram')
    def test_process_document_zip_slip_blocked(self, mock_send, mock_get_file, mock_send_file):
        chat_id = 999
        doc = {"file_id": "zip_id", "file_name": "evil.zip", "mime_type": "application/zip"}

        # Create a real zip file with a path traversal entry
        zip_path = os.path.join(watermarker.TEMP_DIR, "evil.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("../../etc/passwd", "evil content")

        mock_get_file.return_value = zip_path

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

    # --- Zip naming and error reporting tests ---

    def _make_zip(self, name, entries):
        zip_path = os.path.join(watermarker.TEMP_DIR, name)
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for entry_name, data in entries.items():
                zf.writestr(entry_name, data)
        return zip_path

    def _image_bytes(self):
        buf = BytesIO()
        Image.new('RGB', (64, 64), color='blue').save(buf, format='PNG')
        return buf.getvalue()

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_process_document_zip_keeps_original_name(self, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("1234.zip", {"a.png": self._image_bytes()})
        sent = {}

        def capture(bot_token, chat_id, path, file_name, caption=""):
            with zipfile.ZipFile(path) as zf:
                sent["entries"] = zf.namelist()
            sent["name"] = file_name

        mock_send_doc.side_effect = capture
        doc = {"file_id": "zip_id", "file_name": "My Photos 2026.zip", "mime_type": "application/zip"}
        watermarker.process_document("token", 123, doc)

        self.assertEqual(sent["name"], "My Photos 2026.zip")
        self.assertEqual(sent["entries"], ["a.webp"])
        # Temp files cleaned up
        self.assertEqual(os.listdir(watermarker.TEMP_DIR), [])

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_process_document_out_of_storage_reported(self, mock_download, mock_send_doc, mock_send):
        mock_download.side_effect = OSError(errno.ENOSPC, "No space left on device")
        doc = {"file_id": "zip_id", "file_name": "big.zip", "mime_type": "application/zip"}
        watermarker.process_document("token", 123, doc)

        msg = mock_send.call_args[0][2]
        self.assertIn("Error processing zip", msg)
        self.assertIn("out of storage", msg)
        mock_send_doc.assert_not_called()

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_process_document_upload_failure_reported(self, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("1234.zip", {"a.png": self._image_bytes()})
        mock_send_doc.side_effect = RuntimeError("Telegram refused the upload: Request Entity Too Large")
        doc = {"file_id": "zip_id", "file_name": "big.zip", "mime_type": "application/zip"}
        watermarker.process_document("token", 123, doc)

        msg = mock_send.call_args[0][2]
        self.assertIn("Request Entity Too Large", msg)
        self.assertEqual(os.listdir(watermarker.TEMP_DIR), [])

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_process_document_partial_failure_reported(self, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("1234.zip", {
            "good.png": self._image_bytes(),
            "broken.jpg": b"not an image",
        })
        doc = {"file_id": "zip_id", "file_name": "mixed.zip", "mime_type": "application/zip"}
        watermarker.process_document("token", 123, doc)

        mock_send_doc.assert_called()
        self.assertIn("1 failed", mock_send_doc.call_args[0][4])
        msg = mock_send.call_args[0][2]
        self.assertIn("broken.jpg", msg)

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._download_file')
    def test_process_photo_download_failure_reported(self, mock_download, mock_send):
        with open(watermarker.get_watermark_path(123), 'wb') as f:
            f.write(self._image_bytes())
        mock_download.side_effect = RuntimeError("Telegram refused the download: file is too big")
        watermarker.process_photo("token", 123, [{"file_id": "fid"}])

        msg = mock_send.call_args[0][2]
        self.assertIn("file is too big", msg)

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker.process_text', side_effect=RuntimeError("boom"))
    def test_handle_update_reports_unexpected_error(self, mock_process, mock_send):
        update = {"message": {"chat": {"id": 1}, "text": "/help"}}
        watermarker.handle_update("token", update)
        msg = mock_send.call_args[0][2]
        self.assertIn("boom", msg)

    @patch('watermarker.requests.get')
    def test_download_file_raises_with_telegram_reason(self, mock_get):
        mock_get.return_value.json.return_value = {"ok": False, "description": "Bad Request: file is too big"}
        with self.assertRaises(RuntimeError) as ctx:
            watermarker._download_file("token", "fid")
        self.assertIn("file is too big", str(ctx.exception))

    @patch('watermarker.requests.post')
    def test_send_document_raises_on_telegram_error(self, mock_post):
        path = os.path.join(watermarker.TEMP_DIR, "x.zip")
        with open(path, 'wb') as f:
            f.write(b"data")
        mock_post.return_value.json.return_value = {"ok": False, "description": "Request Entity Too Large"}
        with self.assertRaises(RuntimeError) as ctx:
            watermarker._send_document("token", 1, path, "orig.zip")
        self.assertIn("Request Entity Too Large", str(ctx.exception))
        self.assertEqual(mock_post.call_args[1]["files"]["document"][0], "orig.zip")

    # --- Image handling: orientation, colour profile, quality, HEIC ---

    def _wm(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "sun.webp")

    def test_apply_watermark_honors_exif_orientation(self):
        # Stored landscape with "rotate 90" tag, the way cameras save portrait shots
        path = os.path.join(self.test_dir, "portrait.jpg")
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new('RGB', (400, 300), color='green').save(path, exif=exif)
        out = os.path.join(self.test_dir, "portrait_out.webp")
        self.assertTrue(apply_watermark(path, self._wm(), out, position="repeated", size=0.2))
        with Image.open(out) as img:
            self.assertEqual(img.size, (300, 400))

    def test_apply_watermark_honors_exif_orientation_in_webp(self):
        path = os.path.join(self.test_dir, "portrait.webp")
        exif = Image.Exif()
        exif[0x0112] = 8
        Image.new('RGB', (400, 300), color='green').save(path, exif=exif)
        out = os.path.join(self.test_dir, "portrait_out.webp")
        self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2))
        with Image.open(out) as img:
            self.assertEqual(img.size, (300, 400))

    @patch('watermarker_core.ImageOps.exif_transpose', side_effect=SyntaxError("bad EXIF"))
    def test_apply_watermark_survives_broken_exif(self, mock_transpose):
        path = os.path.join(self.test_dir, "broken_exif.jpg")
        Image.new('RGB', (120, 80), color='green').save(path)
        out = os.path.join(self.test_dir, "broken_exif.webp")
        self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2))
        with Image.open(out) as img:
            self.assertEqual(img.size, (120, 80))

    def test_apply_watermark_keeps_icc_profile(self):
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        path = os.path.join(self.test_dir, "profiled.jpg")
        Image.new('RGB', (200, 200), color='red').save(path, icc_profile=icc)
        for ext in (".webp", ".jpg"):
            out = os.path.join(self.test_dir, "profiled_out" + ext)
            self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2))
            with Image.open(out) as img:
                self.assertEqual(img.info.get("icc_profile"), icc)

    def test_apply_watermark_drops_non_rgb_icc_profile(self):
        from PIL import ImageCms
        rgb_icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        # The ICC header's colour-space field (bytes 16-20) is what identifies gray/CMYK profiles
        for mode, space in (("L", b"GRAY"), ("CMYK", b"CMYK")):
            path = os.path.join(self.test_dir, f"{mode}.jpg")
            Image.new(mode, (80, 80)).save(path, icc_profile=rgb_icc[:16] + space + rgb_icc[20:])
            out = os.path.join(self.test_dir, f"{mode}.webp")
            self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2))
            with Image.open(out) as img:
                self.assertEqual(img.mode, "RGB")
                self.assertIsNone(img.info.get("icc_profile"))

    def test_apply_watermark_quality(self):
        path = os.path.join(self.test_dir, "noise.png")
        Image.effect_noise((300, 300), 60).convert("RGB").save(path)
        sizes = {}
        for quality in (30, 95):
            out = os.path.join(self.test_dir, f"q{quality}.webp")
            self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2, quality=quality))
            sizes[quality] = os.path.getsize(out)
        self.assertGreater(sizes[95], sizes[30])

    def test_apply_watermark_tight_spacing(self):
        path = os.path.join(self.test_dir, "base.png")
        Image.new('RGB', (200, 200), color='white').save(path)
        out = os.path.join(self.test_dir, "tight.webp")
        self.assertTrue(apply_watermark(path, self._wm(), out, position="repeated",
                                        size=1.0, x_offset=0.1, y_offset=0.1))

    def test_apply_watermark_rejects_offsets_that_stop_tiling(self):
        path = os.path.join(self.test_dir, "base.png")
        Image.new('RGB', (1200, 900), color='white').save(path)
        out = os.path.join(self.test_dir, "zero.webp")
        for x_offset, y_offset in ((0, 1.0), (1.0, 0), (-1.0, 1.0)):
            start = time.monotonic()
            self.assertFalse(apply_watermark(path, self._wm(), out, position="repeated",
                                             size=0.5, x_offset=x_offset, y_offset=y_offset))
            # Fails immediately instead of pasting at every pixel
            self.assertLess(time.monotonic() - start, 1.0)
        self.assertFalse(os.path.exists(out))

    @unittest.skipUnless(HEIF_SUPPORTED, "pillow-heif not installed")
    def test_apply_watermark_heic_input(self):
        path = os.path.join(self.test_dir, "iphone.heic")
        Image.new('RGB', (200, 100), color='blue').save(path)
        out = os.path.join(self.test_dir, "iphone.webp")
        self.assertTrue(apply_watermark(path, self._wm(), out, size=0.2))
        with Image.open(out) as img:
            self.assertEqual(img.size, (200, 100))
        self.assertIn(".heic", SUPPORTED_EXTS)

    # --- New commands ---

    @patch('watermarker.tele.send_telegram')
    def test_process_text_offsets_and_quality(self, mock_send):
        watermarker.process_text("token", 123, "/x_offset 2.5")
        watermarker.process_text("token", 123, "/y_offset 1.5")
        watermarker.process_text("token", 123, "/quality 95")
        settings = watermarker.load_settings(123)
        self.assertEqual((settings["x_offset"], settings["y_offset"], settings["quality"]), (2.5, 1.5, 95))
        self.assertIsInstance(settings["quality"], int)

    @patch('watermarker.tele.send_telegram')
    def test_process_text_offsets_and_quality_validation(self, mock_send):
        for command in ("/quality 0", "/quality 101", "/x_offset 20", "/y_offset 0"):
            watermarker.process_text("token", 123, command)
            self.assertIn("must be between", mock_send.call_args[0][2])
        watermarker.process_text("token", 123, "/quality high")
        self.assertIn("Invalid number", mock_send.call_args[0][2])
        watermarker.process_text("token", 123, "/x_offset")
        self.assertIn("Usage: /x_offset", mock_send.call_args[0][2])
        self.assertEqual(watermarker.load_settings(123)["quality"], 80)

    @patch('watermarker.tele.send_telegram')
    def test_help_lists_new_commands(self, mock_send):
        watermarker.process_text("token", 123, "/help")
        for command in ("/x_offset", "/y_offset", "/quality"):
            self.assertIn(command, mock_send.call_args[0][2])
            self.assertIn(command[1:], watermarker.BOT_COMMANDS)

    # --- Zips: OS junk, non-image files, parallel processing ---

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_zip_ignores_os_junk_and_reports_skipped_files(self, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("junk.zip", {
            "a.png": self._image_bytes(),
            "__MACOSX/._a.png": b"AppleDouble metadata",
            "._b.jpg": b"AppleDouble metadata",
            ".DS_Store": b"junk",
            "notes.txt": b"hello",
        })
        sent = {}

        def capture(bot_token, chat_id, path, file_name, caption=""):
            with zipfile.ZipFile(path) as zf:
                sent["entries"] = zf.namelist()
            sent["caption"] = caption

        mock_send_doc.side_effect = capture
        watermarker.process_document("token", 123, {"file_id": "z", "file_name": "p.zip"})

        self.assertEqual(sent["entries"], ["a.webp"])
        self.assertEqual(sent["caption"], "Watermarked 1 image(s). Skipped 1 non-image file(s).")
        # Junk isn't reported as failed images
        for call in mock_send.call_args_list:
            self.assertNotIn("Could not process", call[0][2])

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    @patch('watermarker._worker_count', return_value=4)
    def test_zip_parallel_processing_reports_each_result(self, mock_workers, mock_download, mock_send_doc, mock_send):
        entries = {f"img{i}.png": self._image_bytes() for i in range(6)}
        entries["broken.jpg"] = b"not an image"
        mock_download.return_value = self._make_zip("many.zip", entries)
        sent = {}

        def capture(bot_token, chat_id, path, file_name, caption=""):
            with zipfile.ZipFile(path) as zf:
                sent["entries"] = sorted(zf.namelist())
            sent["caption"] = caption

        mock_send_doc.side_effect = capture
        watermarker.process_document("token", 123, {"file_id": "z", "file_name": "many.zip"})

        self.assertEqual(sent["entries"], [f"img{i}.webp" for i in range(6)])
        self.assertEqual(sent["caption"], "Watermarked 6 image(s). 1 failed.")
        self.assertIn("broken.jpg", mock_send.call_args[0][2])

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    @patch('watermarker._worker_count', return_value=4)
    def test_zip_same_name_images_get_distinct_outputs(self, mock_workers, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("dupes.zip", {
            "photo.jpg": self._image_bytes(),
            "photo.png": self._image_bytes(),
            "photo-1.webp": self._image_bytes(),
            "Photo.PNG": self._image_bytes(),  # clashes on case-insensitive file systems
            "sub/photo.png": self._image_bytes(),  # other folder: no clash
        })
        sent = {}

        def capture(bot_token, chat_id, path, file_name, caption=""):
            with zipfile.ZipFile(path) as zf:
                sent["entries"] = sorted(n for n in zf.namelist() if not n.endswith("/"))
            sent["caption"] = caption

        mock_send_doc.side_effect = capture
        watermarker.process_document("token", 123, {"file_id": "z", "file_name": "dupes.zip"})

        self.assertEqual(sent["entries"], ["Photo.webp", "photo-1.webp", "photo-2.webp", "photo-3.webp", "sub/photo.webp"])
        self.assertEqual(sent["caption"], "Watermarked 5 image(s).")

    def test_unique_output_path(self):
        taken = set()
        names = [watermarker._unique_output_path(n, taken) for n in ("a.jpg", "a.png", "A.tif", "b/a.jpg")]
        self.assertEqual(names, ["a.webp", "a-1.webp", "A-2.webp", os.path.join("b", "a") + ".webp"])

    # --- Worker count: bounded by CPU and memory ---

    def test_estimate_job_mb(self):
        path = os.path.join(self.test_dir, "est.png")
        Image.new('RGB', (4000, 3000), color='white').save(path)  # 12 MP
        mb = watermarker._estimate_job_mb(path, 8000000)
        self.assertAlmostEqual(mb, (12e6 * 8 + 8e6 * 56) / 2**20, places=3)
        self.assertAlmostEqual(watermarker._estimate_job_mb(path, None), 12e6 * 64 / 2**20, places=3)

    def test_estimate_job_mb_covers_measured_8mp_peak(self):
        # Measured peak for an 8MP photo with the bot's defaults is ~435-450 MB;
        # the estimate must not come in under it, or an extra worker gets scheduled
        path = os.path.join(self.test_dir, "8mp.png")
        Image.new('RGB', (3266, 2449), color='white').save(path)
        self.assertGreater(watermarker._estimate_job_mb(path, 8000000), 450)
        self.assertEqual(watermarker._estimate_job_mb(os.path.join(self.test_dir, "missing.png"), None), 0)

    @patch('watermarker.os.cpu_count', return_value=4)
    def test_worker_count_bounded_by_lambda_memory(self, mock_cpus):
        # Use the estimator's own figure for an 8MP job, not a hand-picked one
        path = os.path.join(self.test_dir, "8mp.png")
        Image.new('RGB', (3266, 2449), color='white').save(path)
        job_mb = watermarker._estimate_job_mb(path, 8000000)
        cases = {"512": 1, "1024": 1, "1536": 2, "2048": 3, "10240": 4}
        for memory, expected in cases.items():
            with patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_MEMORY_SIZE": memory}):
                self.assertEqual(watermarker._worker_count(job_mb), expected, memory)
        with patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "1024"}):
            self.assertEqual(watermarker._worker_count(200), 4)
            # An unreadable image (estimate 0) doesn't unlock extra workers
            self.assertEqual(watermarker._worker_count(0), 1)

    @patch('watermarker._available_memory_mb', return_value=None)
    @patch('watermarker.os.cpu_count', return_value=8)
    def test_worker_count_sequential_when_memory_unknown(self, mock_cpus, mock_mem):
        self.assertEqual(watermarker._worker_count(450), 1)

    @patch('watermarker.os.cpu_count', return_value=2)
    def test_worker_count_off_lambda_uses_meminfo(self, mock_cpus):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", None)
            meminfo = "MemTotal: 16000000 kB\nMemAvailable:     716800 kB\n"
            with patch('builtins.open', unittest.mock.mock_open(read_data=meminfo)):
                self.assertEqual(watermarker._available_memory_mb(), 700)
                self.assertEqual(watermarker._worker_count(450), 1)

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_zip_with_only_other_files(self, mock_download, mock_send_doc, mock_send):
        mock_download.return_value = self._make_zip("docs.zip", {"a.txt": b"x"})
        watermarker.process_document("token", 123, {"file_id": "z", "file_name": "docs.zip"})
        mock_send_doc.assert_not_called()
        self.assertEqual(mock_send.call_args[0][2], "No images found in zip. Skipped 1 non-image file(s).")

    # --- Image files sent as documents ---

    @patch('watermarker._send_document')
    @patch('watermarker._download_file')
    def test_image_file_is_watermarked_and_returned_as_file(self, mock_download, mock_send_doc):
        path = os.path.join(watermarker.TEMP_DIR, "555.jpg")
        Image.new('RGB', (320, 240), color='orange').save(path)
        mock_download.return_value = path
        sent = {}

        def capture(bot_token, chat_id, out_path, file_name, caption=""):
            with Image.open(out_path) as img:
                sent.update(format=img.format, size=img.size, name=file_name)

        mock_send_doc.side_effect = capture
        doc = {"file_id": "f", "file_name": "DSC01234.JPG", "mime_type": "image/jpeg"}
        watermarker.process_document("token", 123, doc)

        self.assertEqual(sent, {"format": "WEBP", "size": (320, 240), "name": "DSC01234.webp"})
        self.assertEqual(os.listdir(watermarker.TEMP_DIR), [])

    @patch('watermarker.tele.send_telegram')
    def test_unsupported_document_gets_a_reply(self, mock_send):
        doc = {"file_id": "f", "file_name": "report.pdf", "mime_type": "application/pdf"}
        watermarker.process_document("token", 123, doc)
        self.assertIn("Unsupported file type", mock_send.call_args[0][2])

    # --- /source by uploading an image ---

    @patch('watermarker.process_document')
    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._download_file')
    def test_source_caption_sets_watermark_from_file(self, mock_download, mock_send, mock_process_doc):
        path = os.path.join(watermarker.TEMP_DIR, "logo.png")
        Image.new('RGBA', (50, 20), color=(255, 0, 0, 128)).save(path)
        mock_download.return_value = path
        update = {"message": {"chat": {"id": 42}, "caption": "/source",
                              "document": {"file_id": "f", "file_name": "logo.png", "mime_type": "image/png"}}}
        watermarker.handle_update("token", update)

        mock_process_doc.assert_not_called()
        self.assertEqual(mock_send.call_args[0][2], "Watermark set successfully!")
        with Image.open(watermarker.get_watermark_path(42)) as img:
            self.assertEqual((img.size, img.mode), ((50, 20), "RGBA"))
        self.assertFalse(os.path.exists(path))

    @patch('watermarker.process_photo')
    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._download_file')
    def test_source_caption_on_photo(self, mock_download, mock_send, mock_process_photo):
        path = os.path.join(watermarker.TEMP_DIR, "logo.jpg")
        Image.new('RGB', (50, 20), color='red').save(path)
        mock_download.return_value = path
        update = {"message": {"chat": {"id": 43}, "caption": " /source ",
                              "photo": [{"file_id": "small"}, {"file_id": "large"}]}}
        watermarker.handle_update("token", update)

        mock_process_photo.assert_not_called()
        mock_download.assert_called_with("token", "large")
        self.assertTrue(os.path.exists(watermarker.get_watermark_path(43)))

    @patch('watermarker.tele.send_telegram')
    @patch('watermarker._download_file')
    def test_source_caption_rejects_non_image(self, mock_download, mock_send):
        path = os.path.join(watermarker.TEMP_DIR, "notes.txt")
        with open(path, "w") as f:
            f.write("not an image")
        mock_download.return_value = path
        update = {"message": {"chat": {"id": 44}, "caption": "/source",
                              "document": {"file_id": "f", "file_name": "notes.txt"}}}
        watermarker.handle_update("token", update)

        self.assertIn("isn't an image", mock_send.call_args[0][2])
        self.assertFalse(os.path.exists(watermarker.get_watermark_path(44)))

if __name__ == '__main__':
    unittest.main()
