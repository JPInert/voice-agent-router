#!/usr/bin/env python3
"""Compact machine-state snapshot for the curator.

Gathers, in <=2 s, what a troubleshooter needs to know before it starts:
audio sinks/sources + volumes, bluetooth adapters + paired/connected devices,
printers, displays, temperatures, disks, load/mem, failed systemd units and
which tools exist.  Every probe runs in a thread with its own short timeout,
so a wedged `bluetoothctl` costs the deadline and nothing more.

Used two ways:
  * `ladder.py` injects the result into the rung-1 classifier prompt and the
    rung-2/3 system prompt, so "what's my volume" or "is the printer online"
    is answered with zero tool calls.
  * `python3 -m router.snapshot` prints it.

Cache: /tmp/curator-snapshot.txt (also the hand-off to any other process).
"""
import concurrent.futures
import os
import re
import shutil
import subprocess
import time

CACHE = '/tmp/curator-snapshot.txt'
TOOLS = ['pactl', 'bluetoothctl', 'lp', 'lpstat', 'xrandr', 'sensors',
         'nmcli', 'flatpak', 'apt', 'kitty', 'xdotool', 'yt-dlp']
DEADLINE = 2.0


def _run(argv, timeout=1.5):
    """Run argv, return stdout (str) or '' — never raises."""
    try:
        env = dict(os.environ)
        env.setdefault('DISPLAY', ':0')
        env['LC_ALL'] = 'C'
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout, env=env)
        return out.stdout or ''
    except Exception:
        return ''


def _audio():
    if not shutil.which('pactl'):
        return None
    lines = []
    default_sink = _run(['pactl', 'get-default-sink']).strip()
    default_src = _run(['pactl', 'get-default-source']).strip()
    for kind, cmd in (('sink', 'sinks'), ('source', 'sources')):
        txt = _run(['pactl', 'list', 'short', cmd])
        for ln in txt.splitlines():
            f = ln.split('\t')
            if len(f) < 2:
                continue
            name = f[1]
            if kind == 'source' and name.endswith('.monitor'):
                continue
            mark = ' (default)' if name in (default_sink, default_src) else ''
            vol = _run(['pactl', f'get-{kind}-volume', name])
            m = re.search(r'(\d+)%', vol)
            mute = 'muted' if 'yes' in _run(['pactl', f'get-{kind}-mute', name]) else ''
            lines.append(f'  {kind} {name}{mark} {m.group(1) + "%" if m else "?"} {mute}'.rstrip())
    return 'AUDIO:\n' + '\n'.join(lines) if lines else None


def _bluetooth():
    if not shutil.which('bluetoothctl'):
        return None
    show = _run(['bluetoothctl', 'show'])
    powered = 'yes' if re.search(r'Powered:\s*yes', show) else 'no'
    lines = [f'  adapter powered={powered}']
    seen = set()
    for label, argv in (('paired', ['bluetoothctl', 'devices', 'Paired']),
                        ('connected', ['bluetoothctl', 'devices', 'Connected'])):
        for ln in _run(argv).splitlines():
            m = re.match(r'Device (\S+) (.*)', ln.strip())
            if not m:
                continue
            key = m.group(1)
            if label == 'paired' and key in seen:
                continue
            seen.add(key)
            lines.append(f'  {label} {m.group(2)} [{m.group(1)}]')
    return 'BLUETOOTH:\n' + '\n'.join(lines)


def _printers():
    if not shutil.which('lpstat'):
        return None
    txt = _run(['lpstat', '-p', '-d'])
    if not txt.strip():
        return 'PRINTERS: none configured'
    body = '\n'.join('  ' + ln.strip() for ln in txt.splitlines() if ln.strip())
    return 'PRINTERS:\n' + body


def _displays():
    if not shutil.which('xrandr'):
        return None
    txt = _run(['xrandr', '--listmonitors'])
    body = '\n'.join('  ' + ln.strip() for ln in txt.splitlines()[1:] if ln.strip())
    return 'DISPLAYS:\n' + body if body else None


def _temps():
    if not shutil.which('sensors'):
        return None
    txt = _run(['sensors'])
    keep = []
    for ln in txt.splitlines():
        if re.search(r'(Package id|Tctl|Composite|temp1|Core 0|edge)\b.*\+\d', ln):
            keep.append('  ' + ' '.join(ln.split()))
    return 'TEMPS:\n' + '\n'.join(keep[:8]) if keep else None


def _disks():
    txt = _run(['df', '-h', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'efivarfs',
                '-x', 'squashfs', '-x', 'overlay'])
    keep = []
    for ln in txt.splitlines()[1:]:
        f = ln.split()
        if len(f) >= 6 and f[0].startswith('/'):
            keep.append(f'  {f[5]} {f[1]} total, {f[3]} free ({f[4]} used)')
    return 'DISKS:\n' + '\n'.join(keep) if keep else None


def _load():
    try:
        la = os.getloadavg()
        cpus = os.cpu_count() or 1
    except Exception:
        return None
    mem = ''
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for ln in f:
                k, _, v = ln.partition(':')
                info[k] = int(v.split()[0])
        tot = info.get('MemTotal', 0) / 1048576.0
        avail = info.get('MemAvailable', 0) / 1048576.0
        mem = f', mem {avail:.1f}G free of {tot:.1f}G'
    except Exception:
        pass
    up = ''
    try:
        with open('/proc/uptime') as f:
            up = f', up {float(f.read().split()[0]) / 3600:.1f}h'
    except Exception:
        pass
    return f'LOAD: {la[0]:.2f} {la[1]:.2f} {la[2]:.2f} over {cpus} cpus{mem}{up}'


def _failed():
    bad = []
    for scope, argv in (('system', ['systemctl', '--failed', '--no-legend', '--plain']),
                        ('user', ['systemctl', '--user', '--failed', '--no-legend', '--plain'])):
        for ln in _run(argv).splitlines():
            unit = ln.split()[0] if ln.split() else ''
            if unit and unit != '0':
                bad.append(f'  {scope} {unit}')
    return 'FAILED UNITS:\n' + '\n'.join(bad) if bad else 'FAILED UNITS: none'


def _tools():
    have = [t for t in TOOLS if shutil.which(t)]
    missing = [t for t in TOOLS if t not in have]
    s = 'TOOLS: ' + ' '.join(have)
    if missing:
        s += '  (missing: ' + ' '.join(missing) + ')'
    return s


PROBES = [_audio, _bluetooth, _printers, _displays, _temps, _disks, _load,
          _failed, _tools]


def text(deadline=DEADLINE):
    """Gather the snapshot. Returns a compact multi-line string."""
    t0 = time.time()
    parts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROBES)) as ex:
        futs = [(p.__name__, ex.submit(p)) for p in PROBES]
        for name, fut in futs:
            left = deadline - (time.time() - t0)
            try:
                r = fut.result(timeout=max(0.05, left))
            except Exception:
                r = f'{name.strip("_").upper()}: (timed out)'
            if r:
                parts.append(r)
    head = time.strftime('SNAPSHOT %H:%M:%S') + f' (gathered in {time.time() - t0:.1f}s)'
    return head + '\n' + '\n'.join(parts)


def refresh(path=CACHE, deadline=DEADLINE):
    s = text(deadline)
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(s)
        os.replace(tmp, path)
    except Exception:
        pass
    return s


def cached(max_age=300, path=CACHE):
    """Cached snapshot, refreshed if older than max_age seconds."""
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            with open(path) as f:
                return f.read()
    except Exception:
        pass
    return refresh(path)


if __name__ == '__main__':
    print(refresh())
