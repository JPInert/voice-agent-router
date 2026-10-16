"""The owner's phone: timers, alarms, battery, open an app, put a web page on the screen.

Every argument is validated here, before anything is composed into a command: a URL must be a plain
http(s) URL with no shell metacharacters, a timer is 1 to 86400 seconds, an alarm is HH:MM 24-hour,
an app is matched by letters and digits only. `AdbPhone` turns those into Android intents over adb;
the production phone path used root over ssh, which is not portable and is not included.
"""
import re
import shlex
import subprocess

URL_RE = re.compile(r'^https?://[^\s\'"`]+$')


def validate(action, value='', label=''):
    """-> (None, cleaned_value) or (error sentence, None)."""
    if action == 'timer':
        if not str(value).isdigit() or not 0 < int(value) <= 86400:
            return 'Refused: timer wants seconds (1 to 86400).', None
        return None, str(int(value))
    if action == 'alarm':
        m = re.match(r'^(\d{1,2}):(\d{2})$', str(value))
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return 'Refused: alarm wants HH:MM, 24-hour.', None
        return None, f'{int(m[1])}:{m[2]}'
    if action == 'app':
        word = re.sub(r'[^a-z0-9]', '', str(value).lower())
        return (None, word) if word else ('Refused: which app?', None)
    if action == 'battery':
        return None, ''
    return 'Unknown phone action.', None


class AdbPhone:
    """adb-backed phone. `run(argv)` is injectable so the argv can be tested without a device."""

    def __init__(self, adb='adb', serial=None, run=None):
        self.base = [adb] + (['-s', serial] if serial else [])
        self.run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=20))

    def _sh(self, script):
        r = self.run(self.base + ['shell', script])
        return r.returncode == 0, r.stdout

    def open_url(self, url):
        ok, _ = self._sh(f'input keyevent KEYCODE_WAKEUP; am start -a android.intent.action.VIEW -d {shlex.quote(url)}')
        return ok

    def timer(self, seconds, label=''):
        return self._sh('am start -a android.intent.action.SET_TIMER --ei android.intent.extra.alarm.LENGTH '
                        f'{int(seconds)} --es android.intent.extra.alarm.MESSAGE {shlex.quote(label)} '
                        '--ez android.intent.extra.alarm.SKIP_UI true')[0]

    def alarm(self, hhmm, label=''):
        h, m = hhmm.split(':')
        return self._sh('am start -a android.intent.action.SET_ALARM --ei android.intent.extra.alarm.HOUR '
                        f'{int(h)} --ei android.intent.extra.alarm.MINUTES {int(m)} '
                        f'--es android.intent.extra.alarm.MESSAGE {shlex.quote(label)} '
                        '--ez android.intent.extra.alarm.SKIP_UI true')[0]

    def battery(self):
        ok, out = self._sh('dumpsys battery | grep -E "level:|AC powered:|USB powered:|Wireless powered:"')
        return ' '.join(out.split()) if ok else None

    def open_app(self, word):
        ok, out = self._sh(f'pm list packages -3 | cut -d: -f2 | grep -i {shlex.quote(word)} | head -1')
        pkg = out.strip()
        if not (ok and re.match(r'^[\w.]+$', pkg)):
            return None
        return pkg if self._sh(f'monkey -p {pkg} -c android.intent.category.LAUNCHER 1')[0] else None


def phone_do(backend, action, value='', label=''):
    err, value = validate(action, value, label)
    if err:
        return err
    label = str(label or '')[:60]
    if action == 'battery':
        b = backend.battery()
        return f'Phone battery: {b}' if b else "Couldn't read the phone."
    if action == 'app':
        return 'Opened it.' if backend.open_app(value) else f'No installed app matches "{value}".'
    ok = backend.timer(value, label) if action == 'timer' else backend.alarm(value, label)
    return 'Done on the phone.' if ok else "The phone didn't take it."


def open_url(backend, url):
    if not URL_RE.match(url or ''):
        return 'Refused: not a plain web URL.'
    return 'Opened on the phone.' if backend.open_url(url) else "The phone didn't open it."


def register(reg, backend):
    reg.add('phone_do', "On the owner's phone: action=timer (value=seconds), alarm (value=HH:MM 24h), battery, "
                        "app (value=app name). label = optional timer/alarm name.",
            {'action': str, 'value': str, 'label': str},
            lambda action, value='', label='': phone_do(backend, action, value, label),
            writes=lambda a: a.get('action') in ('timer', 'alarm', 'app'), source='phone')
    reg.add('phone_open', "Open a web URL on the phone's screen (pictures, pages, maps, videos).", {'url': str},
            lambda url: open_url(backend, url), writes=True, source='phone')
