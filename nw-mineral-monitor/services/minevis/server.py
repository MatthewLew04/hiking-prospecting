#!/usr/bin/env python3
"""minevis.server — the HTTP surface an agent on this box talks to.

    GET  /tools        the OpenAI function-schema array, ready to paste into
                       the agent's `tools` parameter
    POST /call         {"name": "...", "arguments": {...}}
                       -> the tool's result, or {"job_id": "..."}
    GET  /jobs/<id>    {"state": queued|running|done|questions|error, ...}
    GET  /healthz      liveness, for systemd
    GET  /models/...   only when no bucket is configured: serves the models
                       this service wrote to local disk, so the whole loop is
                       provable with no AWS at all

Binding to 127.0.0.1 is the default and is the security model: the agent runs
on the same box.  Set ``MINEVIS_TOKEN`` when the agent lives in a different
container on the same host, and it must then send ``X-MineVis-Token``.

Run it:  python3 services/minevis/server.py --state-dir /var/lib/minevis
"""
import argparse
import json
import os
import posixpath
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

from minevis import __version__, jobs as jobs_mod, tools as tools_mod  # noqa: E402

MAX_BODY = 2 * 1024 * 1024                     # a mine description, not a corpus
DRAIN_CAP = 16 * 1024 * 1024                   # read-and-discard limit before hanging up

SERVE_TYPES = {'.json': 'application/json', '.geojson': 'application/geo+json',
               '.svg': 'image/svg+xml', '.omf': 'application/octet-stream',
               '.dxf': 'application/dxf'}


class Service(object):
    """The wiring: stores, tool context, and the token if there is one."""

    def __init__(self, state_dir, workers=2, token=None, base_url=None, zoom=13,
                 offline=False, target=None, log=print):
        self.state_dir = os.path.abspath(state_dir)
        self.token = token or os.environ.get('MINEVIS_TOKEN') or None
        self.log = log
        self.specs = jobs_mod.SpecStore(self.state_dir)
        self.jobs = jobs_mod.JobStore(self.state_dir, workers=workers, log=log)
        self.ctx = tools_mod.Context(self.jobs, self.specs, target=target, base_url=base_url,
                                     zoom=zoom, offline=offline, log=log)
        self.jobs.runner = tools_mod.make_runner(self.ctx)
        picked = self.jobs.resume()
        if picked:
            log('resumed %d job(s) left over from the last run: %s' % (len(picked), ', '.join(picked)))

    def close(self):
        self.jobs.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'minevis/%s' % __version__
    protocol_version = 'HTTP/1.1'
    service = None

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        self.service.log('%s %s' % (self.address_string(), fmt % args))

    def _send(self, code, payload, content_type='application/json', raw=None):
        body = raw if raw is not None else json.dumps(payload, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _error(self, code, error, detail=''):
        self._send(code, {'error': error, 'detail': detail})

    def _authorised(self):
        if not self.service.token:
            return True
        return self.headers.get('X-MineVis-Token') == self.service.token

    # --------------------------------------------------------------- routes
    def do_GET(self):                                   # noqa: N802 - http.server API
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if path == '/healthz':
            return self._send(200, {'ok': True, 'version': __version__,
                                    'storage': self.service.ctx.target_kind})
        if not self._authorised():
            return self._error(401, 'unauthorised', 'send the X-MineVis-Token header')
        if path == '/tools':
            return self._send(200, tools_mod.TOOLS)
        if path.startswith('/jobs/'):
            return self._call({'name': 'get_job', 'arguments': {'job_id': path[len('/jobs/'):]}})
        if path.startswith('/models/'):
            return self._serve_model(path)
        return self._error(404, 'not_found', '%s is not a route; try /tools' % path)

    def do_HEAD(self):                                  # noqa: N802
        return self.do_GET()

    def do_POST(self):                                  # noqa: N802
        if not self._authorised():
            return self._error(401, 'unauthorised', 'send the X-MineVis-Token header')
        if self.path.split('?', 1)[0].rstrip('/') != '/call':
            return self._error(404, 'not_found', 'POST /call is the only POST route')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self._error(400, 'bad_request', 'Content-Length is not a number')
        if length > MAX_BODY:
            # An over-long body still has to be taken off the socket, or the
            # client sees a broken pipe instead of the 413 explaining itself.
            self._drain(length)
            self.close_connection = True
            return self._error(413, 'too_large', 'the body must be under %d bytes' % MAX_BODY)
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(400, 'bad_json', str(exc))
        return self._call(body)

    def _drain(self, length):
        left = min(length, DRAIN_CAP)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)

    def _call(self, body):
        if not isinstance(body, dict):
            return self._error(400, 'bad_request', 'the body must be a JSON object')
        name = body.get('name')
        args = body.get('arguments')
        if args is None:
            args = {}
        try:
            kind, payload = tools_mod.dispatch(name, args, self.service.ctx)
        except tools_mod.ToolError as exc:
            return self._error(400, 'bad_call', str(exc))
        except Exception as exc:                        # never leak a traceback to the agent
            self.service.log('tool %s failed: %s: %s' % (name, type(exc).__name__, exc))
            return self._error(500, 'tool_failed', '%s: %s' % (type(exc).__name__, exc))
        return self._send(202 if kind == 'job' else 200, payload)

    def _serve_model(self, path):
        """Local-disk models only.  With a bucket configured, CloudFront serves
        these and this route is switched off."""
        if self.service.ctx.target_kind != 'local':
            return self._error(404, 'not_found',
                               'models are served from the bucket, not from this service')
        rel = posixpath.normpath(path.lstrip('/'))
        if rel.startswith('..') or os.path.isabs(rel):
            return self._error(400, 'bad_path', 'no')
        full = os.path.join(self.service.ctx.local_models, *rel.split('/')[1:])
        root = os.path.abspath(self.service.ctx.local_models)
        if os.path.commonpath([root, os.path.abspath(full)]) != root:
            return self._error(400, 'bad_path', 'no')
        try:
            with open(full, 'rb') as fh:
                data = fh.read()
        except (OSError, IOError):
            return self._error(404, 'not_found', rel)
        return self._send(200, None, SERVE_TYPES.get(os.path.splitext(full)[1],
                                                     'application/octet-stream'), raw=data)


def make_server(service, host='127.0.0.1', port=8787):
    handler = type('BoundHandler', (Handler,), {'service': service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1',
                    help='bind address (default loopback: the agent is on this box)')
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--state-dir', default=os.environ.get('MINEVIS_STATE', '/var/lib/minevis'),
                    help='where jobs, parsed specs and (with no bucket) models are kept')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--base-url', default=None, help='public site URL (or NWMM_SITE_URL)')
    ap.add_argument('--zoom', type=int, default=13, help='terrain tile zoom')
    ap.add_argument('--offline', action='store_true', help='never fetch terrain tiles')
    args = ap.parse_args(argv)

    service = Service(args.state_dir, workers=args.workers, base_url=args.base_url,
                      zoom=args.zoom, offline=args.offline)
    httpd = make_server(service, args.host, args.port)
    print('minevis %s on http://%s:%d  state=%s  storage=%s  auth=%s'
          % (__version__, args.host, args.port, service.state_dir,
             service.ctx.target_kind, 'token' if service.token else 'loopback only'))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
