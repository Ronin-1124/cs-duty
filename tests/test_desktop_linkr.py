import io
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from cs_duty.desktop import LinkrClient, find_windows
from cs_duty.desktop.linkr import _content_box


def frame_bytes(width=1600, height=1000, box=(100, 50, 1499, 899)):
    image = Image.new('RGB', (width, height), 'black')
    ImageDraw.Draw(image).rectangle(box, fill='white')
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content=b'', payload=None):
        self.content = content
        self._payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, content=b''):
        self.content = content
        self.posts = []

    def get(self, url, headers=None):
        return FakeResponse(content=self.content)

    def post(self, url, headers=None, json=None):
        self.posts.append({'url': url, 'json': json})
        return FakeResponse(payload={'code': 0})

    def close(self):
        return None


class ContentBoxTests(unittest.TestCase):
    def test_black_bars_are_trimmed_without_desktop_size(self):
        image = Image.open(io.BytesIO(frame_bytes()))
        self.assertEqual(_content_box(image), (100, 50, 1500, 900))

    def test_uniform_frame_keeps_full_image(self):
        image = Image.new('RGB', (320, 200), 'black')
        self.assertEqual(_content_box(image), (0, 0, 320, 200))

    def test_mostly_dark_frame_is_not_collapsed(self):
        image = Image.new('RGB', (400, 300), 'black')
        ImageDraw.Draw(image).rectangle((200, 0, 399, 299), fill='white')
        self.assertEqual(_content_box(image), (0, 0, 400, 300))

    def test_desktop_size_gives_geometric_letterbox(self):
        image = Image.new('RGB', (2560, 1440), 'black')
        self.assertEqual(_content_box(image, (2560, 1600)), (128, 0, 2432, 1440))


class LinkrClientTests(unittest.TestCase):
    def client(self, content=b''):
        client = LinkrClient(base_url='http://linkr.test', token='fixture')
        client.http = FakeHttp(content)
        return client

    def test_token_is_required(self):
        with patch.dict('os.environ', {'LINKR_TOKEN': ''}):
            with self.assertRaisesRegex(RuntimeError, 'LINKR_TOKEN'):
                LinkrClient(base_url='http://linkr.test', token=None)

    def test_snapshot_maps_a_full_desktop_window_into_the_letterbox(self):
        client = self.client(frame_bytes())
        with patch('cs_duty.desktop.linkr._desktop_size', return_value=(2000, 1000)):
            shot = client.snapshot(window_rect=(0, 0, 2000, 1000))
        image = Image.open(io.BytesIO(shot.png))
        self.assertEqual(image.size, (1600, 800))
        self.assertEqual((shot.left, shot.top), (0, 100))
        self.assertEqual((shot.width, shot.height), (1600, 800))
        self.assertEqual((shot.frame_width, shot.frame_height), (1600, 1000))
        self.assertEqual((shot.content_left, shot.content_top), (0, 100))
        self.assertEqual((shot.content_width, shot.content_height), (1600, 800))

    def test_snapshot_without_window_keeps_the_full_frame(self):
        client = self.client(frame_bytes())
        with patch('cs_duty.desktop.linkr._desktop_size', return_value=(1600, 1000)):
            shot = client.snapshot()
        image = Image.open(io.BytesIO(shot.png))
        self.assertEqual(image.size, (1600, 1000))
        self.assertEqual((shot.left, shot.top), (0, 0))

    def test_snapshot_maps_a_partial_window_into_the_letterbox(self):
        client = self.client(frame_bytes())
        with patch('cs_duty.desktop.linkr._desktop_size', return_value=(2000, 1000)):
            shot = client.snapshot(window_rect=(0, 0, 1000, 500))
        image = Image.open(io.BytesIO(shot.png))
        self.assertEqual(image.size, (800, 400))
        self.assertEqual((shot.left, shot.top), (0, 100))

    def test_click_sends_press_release_sequence(self):
        client = self.client()
        client.click(.5, .25)
        events = client.http.posts[0]['json']['events']
        self.assertEqual(events, [
            ['mouse_abs', 0, .5, .25, 0, 0],
            ['delay', 40],
            ['mouse_abs', 1, .5, .25, 0, 0],
            ['delay', 40],
            ['mouse_abs', 0, .5, .25, 0, 0],
        ])

    def test_click_clamps_normalized_coordinates(self):
        client = self.client()
        client.click(-1, 2)
        events = client.http.posts[0]['json']['events']
        self.assertEqual(events[0][2:4], [0.0, 1.0])

    def test_click_image_maps_cropped_pixel_to_desktop(self):
        client = self.client(frame_bytes())
        with patch('cs_duty.desktop.linkr._desktop_size', return_value=(2000, 1000)):
            shot = client.snapshot(window_rect=(0, 0, 2000, 1000))
        normalized = client.click_image(shot, 350, 200)
        self.assertEqual(normalized, (350 / 1600, 200 / 800))

    def test_click_image_on_full_frame_accounts_for_the_letterbox(self):
        client = self.client(frame_bytes())
        with patch('cs_duty.desktop.linkr._desktop_size', return_value=(2000, 1000)):
            shot = client.snapshot()
        normalized = client.click_image(shot, 800, 550)
        self.assertEqual(normalized, (800 / 1600, (550 - 100) / 800))

    def test_paste_sends_control_v(self):
        client = self.client()
        client.paste()
        events = client.http.posts[0]['json']['events']
        self.assertEqual(events[0], ['keyboard', 'ControlLeft', True])
        self.assertIn(['keyboard', 'KeyV', True], events)
        self.assertIn(['keyboard', 'KeyV', False], events)
        self.assertEqual(events[-1], ['keyboard', 'ControlLeft', False])

    def test_control_failure_raises(self):
        client = self.client()
        client.http.post = lambda url, headers=None, json=None: FakeResponse(payload={'code': 7, 'message': 'busy'})
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            client.click(.5, .5)


class WindowLookupTests(unittest.TestCase):
    def test_missing_process_returns_empty(self):
        self.assertEqual(find_windows(process='cs-duty-no-such-process'), [])


if __name__ == '__main__':
    unittest.main()
