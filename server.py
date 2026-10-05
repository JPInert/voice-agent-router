#!/usr/bin/env python3
"""The curator as a service: POST /utterance {"text": "..."} from the voice daemon.

Responds within ~1 s (the daemon's main loop is blocked on it):
  {"ack": "<answer to speak>"}  rung 1 answered inside the deadline
  {"ack": ""}                   work continues in the background; the daemon plays an earcon
  {"ack": null}                 error — the daemon falls back to its own path

GET /health -> {"ok": true}. A green /health proves the HTTP thread is alive and
nothing else: see "The three-day lock-up" in the README for why the loop probe exists.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from router.ladder import Router
from router.snapshot import text as machine_snapshot
from router.voice_io import HttpVoice

PORT = int(os.environ.get('CURATOR_PORT', '7782'))
ROUTER = Router(HttpVoice(), snapshot=machine_snapshot)


async def _noop():
    return True


def loop_alive(timeout=2.0):
    """A real liveness probe: schedule a no-op on the SDK loop and wait for it."""
    try:
        return ROUTER.submit(_noop()).result(timeout=timeout)
    except Exception:
        return False


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            t0 = time.time()
            ok = loop_alive()
            self._send(200 if ok else 503, {'ok': ok, 'loop_ms': round((time.time() - t0) * 1000)})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/utterance':
            return self._send(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            text = (json.loads(self.rfile.read(n) or b'{}').get('text') or '').strip()
        except Exception:
            return self._send(400, {'error': 'bad json'})
        if not text:
            return self._send(400, {'error': 'no text'})
        self._send(200, {'ack': ROUTER.utterance(text)})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print(f'curator listening on 127.0.0.1:{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
