#!/usr/bin/env python3
"""The phone endpoint: POST /phone, POST /phone/pending, GET /health on 127.0.0.1.

    POST /phone          {"text": "...", "who": "owner", "away": false, "no_claude": false}
                         `who` is advisory (the client says it); a missing `who` is a guest.
                         The bearer token is the real boundary: anyone holding it can claim "owner".
                         -> tier 0 first; a miss from the owner starts the worker:
                            {"what": "claude", "said": ["On it."], "pending": "<id>"}
    POST /phone/pending  {"id": "<id>"}  -> the worker's {"sentence", "tools", "secs"}
                         (blocks up to 80 s; "Still working on it." if it is not done)

Every request needs `Authorization: Bearer $ROUTER_PHONE_TOKEN`. The server binds to loopback only;
reach it from a phone over a VPN you already run, never an open port.

    ROUTER_PHONE_TOKEN=... python3 phone_server.py --fake-backends     # real model, fake devices
    ROUTER_PHONE_TOKEN=... ROUTER_HA_URL=... ROUTER_HA_TOKEN=... python3 phone_server.py

Backends are read from the environment (none set = that connector is simply not registered):
ROUTER_HA_URL + ROUTER_HA_TOKEN, ROUTER_KODI_URL, ROUTER_CAR_CMD (a command that prints JSON, e.g.
"python3 /path/to/car.py"), ROUTER_ADB_SERIAL (enables the adb phone adapter).
"""
import argparse
import hmac
import json
import os
import shlex
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from router import fakes, tricks
from router.connectors.car import CliCarBackend
from router.connectors.home import HttpHome, Reflex
from router.connectors.kodi import HttpKodi
from router.connectors.phone import AdbPhone
from router.phone_jobs import PhoneJobs, handle_phone
from router.phone_worker import PhoneWorker, build_registry, default_policy

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('ROUTER_PHONE_PORT', '7783'))
OWNER = 'owner'


def make_worker(use_fakes, tricks_path):
    loader = tricks.Loader(tricks_path)
    if use_fakes:
        home, kodi_b, car_b, phone_b = fakes.FakeHome(), fakes.FakeKodi(), fakes.FakeCar(), fakes.FakePhone()
        mail_b, cal_b, food_b = fakes.FakeMail(), fakes.FakeCalendar(), fakes.FakeFood()
    else:
        env = os.environ
        home = HttpHome(env['ROUTER_HA_URL'], env['ROUTER_HA_TOKEN']) if env.get('ROUTER_HA_TOKEN') else None
        kodi_b = HttpKodi(env['ROUTER_KODI_URL']) if env.get('ROUTER_KODI_URL') else None
        car_b = CliCarBackend(shlex.split(env['ROUTER_CAR_CMD'])) if env.get('ROUTER_CAR_CMD') else None
        phone_b = AdbPhone(serial=env['ROUTER_ADB_SERIAL']) if env.get('ROUTER_ADB_SERIAL') else None
        mail_b = cal_b = food_b = None
    reflex = Reflex(loader, ha=home)
    reg = build_registry(home_backend=home, reflex=reflex, kodi_backend=kodi_b, car_backend=car_b,
                         mail_backend=mail_b, calendar_backend=cal_b, phone_backend=phone_b,
                         food_backend=food_b, wishlist_path=os.path.join(HERE, 'wishlist.jsonl'))
    return reflex, PhoneWorker(reg, default_policy(reg, hosted_connectors=not use_fakes))


class H(BaseHTTPRequestHandler):
    reflex = jobs = bearer = None

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {'ok': True}) if self.path == '/health' else self._send(404, {'error': 'not found'})

    def do_POST(self):
        got = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
        if not self.bearer or not hmac.compare_digest(got, self.bearer):
            return self._send(401, {'error': 'unauthorized'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) if 0 < n < 16384 else b'{}')
        except Exception:
            return self._send(400, {'error': 'bad json'})
        if self.path == '/phone':
            return self._send(200, handle_phone(req.get('text'), req.get('who') or 'guest', self.reflex, self.jobs,
                                                owner=OWNER, no_claude=bool(req.get('no_claude')),
                                                away=bool(req.get('away'))))
        if self.path == '/phone/pending':
            res = self.jobs.collect(str(req.get('id') or ''))
            return self._send(200 if res else 404, res or {'error': 'unknown id'})
        self._send(404, {'error': 'not found'})

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fake-backends', action='store_true', help='fake home/car/phone/mail: nothing real is touched')
    ap.add_argument('--tricks', default=os.path.join(HERE, 'examples', 'tricks.yaml'))
    a = ap.parse_args()
    token = os.environ.get('ROUTER_PHONE_TOKEN', '')
    if not token:
        sys.exit('set ROUTER_PHONE_TOKEN (any long random string); requests without it are refused')
    reflex, worker = make_worker(a.fake_backends, a.tricks)
    H.reflex, H.jobs, H.bearer = staticmethod(reflex), PhoneJobs(worker), token
    print(f'phone endpoint on 127.0.0.1:{PORT} (fake backends: {a.fake_backends})', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()


if __name__ == '__main__':
    main()
