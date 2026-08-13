import functools
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import sys


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from range_server import RangeRequestHandler  # noqa: E402


class QuietRangeHandler(RangeRequestHandler):
    def log_message(self, *args):
        pass


class RangeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.temp.name, 'archive.pmtiles'), 'wb') as target:
            target.write(b'0123456789')
        handler = functools.partial(QuietRangeHandler, directory=self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/archive.pmtiles'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, byte_range=None):
        headers = {'Range': byte_range} if byte_range else {}
        return urllib.request.urlopen(urllib.request.Request(self.url, headers=headers))

    def test_full_response_advertises_ranges(self):
        with self.get() as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
            self.assertEqual(response.read(), b'0123456789')

    def test_bounded_open_and_suffix_ranges(self):
        for header, expected, content_range in (
                ('bytes=2-5', b'2345', 'bytes 2-5/10'),
                ('bytes=7-', b'789', 'bytes 7-9/10'),
                ('bytes=-3', b'789', 'bytes 7-9/10')):
            with self.subTest(header=header), self.get(header) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers['Content-Range'], content_range)
                self.assertEqual(response.read(), expected)

    def test_invalid_range_is_416(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('bytes=99-100')
        self.assertEqual(error.exception.code, 416)
        self.assertEqual(error.exception.headers['Content-Range'], 'bytes */10')
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
