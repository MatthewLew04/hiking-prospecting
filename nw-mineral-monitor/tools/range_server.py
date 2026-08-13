#!/usr/bin/env python3
"""Tiny local static server with the byte ranges required by PMTiles.

Python's commonly used ``python -m http.server`` invocation is not reliably
range-capable across supported local Python versions. This server handles a
single byte range and otherwise behaves like SimpleHTTPRequestHandler. It is
for local preview only, not production hosting.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import socket


RANGE = re.compile(r'^bytes=(\d*)-(\d*)$')


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    range_start = None
    range_end = None

    def send_head(self):
        self.range_start = self.range_end = None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            source = open(path, 'rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None
        try:
            stat = os.fstat(source.fileno())
            size = stat.st_size
            header = self.headers.get('Range')
            if not header:
                self.send_response(200)
                self.send_header('Content-type', self.guess_type(path))
                self.send_header('Content-Length', str(size))
                self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                return source
            match = RANGE.fullmatch(header.strip())
            if not match or size == 0:
                raise ValueError
            left, right = match.groups()
            if not left:
                length = int(right or '0')
                if length <= 0:
                    raise ValueError
                start, end = max(0, size - length), size - 1
            else:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
                if start >= size or end < start:
                    raise ValueError
            self.range_start, self.range_end = start, end
            self.send_response(206)
            self.send_header('Content-type', self.guess_type(path))
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            source.seek(start)
            return source
        except ValueError:
            source.close()
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            return None

    def copyfile(self, source, outputfile):
        if self.range_start is None:
            return super().copyfile(source, outputfile)
        remaining = self.range_end - self.range_start + 1
        while remaining:
            block = source.read(min(64 * 1024, remaining))
            if not block:
                break
            outputfile.write(block)
            remaining -= len(block)


class RangeHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded preview server tuned for MapLibre's parallel range reads."""
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def server_bind(self):
        super().server_bind()
        # A half-closed keepalive socket can otherwise surface as intermittent
        # ERR_CONNECTION_RESET during Chromium's burst of PMTiles requests.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve the map locally with PMTiles byte ranges')
    parser.add_argument('port', nargs='?', type=int, default=8000)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--directory', default=os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', 'site')))
    args = parser.parse_args(argv)
    handler = functools.partial(RangeRequestHandler, directory=args.directory)
    with RangeHTTPServer((args.bind, args.port), handler) as server:
        print(f'Serving {os.path.realpath(args.directory)} at '
              f'http://{args.bind}:{server.server_port} (Range enabled)')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
