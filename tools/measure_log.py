#!/usr/bin/env python3
"""Reproduce the README numbers from the two production logs.

    python3 tools/measure_log.py voice-input.log [curator.log]

voice-input.log: lines are `MM-DD HH:MM:SS <message>`. A command counts as
MODEL-FREE when one of these outcome lines appears (tier 0 did the work):

    trick '<name>' done (ok=True ...)        a tricks.yaml entry ran
    kodi: Playing ... | kodi: Resuming ...   a title matched the media library
    kodi transport ...                       pause / stop / resume
    lights set to N% | lights scene fired    the lighting grammar
    good night scene fired | good morning scene fired
    tv volume ... | comfort: ... | nav: sending ... | repeat: ...
    ha matched=True                          Home Assistant's own Assist grammar matched

and as SENT TO THE MODEL TIER on `curator ...` (the POST to /utterance).
Outcome lines less than 6 s apart are one command (a chain like "lights 40 and
AC 72" logs several). `[replay]` lines are regression-test replays and are
skipped. `ha matched=False/None` are Assist MISSES, not handled commands.

Undercounts model-free work: fast-lane transport words ("pause", "stop")
are handled before routing and log no outcome line.

curator.log: each utterance that reaches rung 1 logs `ack=silent` when the
classifier missed the 1.05 s deadline and again with `-> {...}` when it
returns; only the second (or `rung1 failed`) is counted.
"""
import collections
import datetime as dt
import re
import sys

FREE = re.compile(r"^(trick '.*' done \(ok=True|kodi: (Playing|Resuming)|kodi transport|"
                  r"lights set to|lights scene fired|good night scene fired|"
                  r"good morning scene fired|tv volume|comfort:|nav: sending|"
                  r"ha matched=True|repeat:)")
MODEL = re.compile(r"^curator ")
LINE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")


def voice(path, year):
    events = []
    with open(path, errors='replace') as f:
        for raw in f:
            m = LINE.match(raw.rstrip())
            if not m:
                continue
            body = m.group(2)
            kind = 'free' if FREE.match(body) else 'model' if MODEL.match(body) else None
            if kind:
                t = dt.datetime.strptime(f'{year}-{m.group(1)}', '%Y-%m-%d %H:%M:%S')
                events.append((t, kind, body))
    cmds = []
    for t, kind, body in events:
        if cmds and (t - cmds[-1]['t']).total_seconds() <= 6:
            cmds[-1]['kinds'].add(kind)
            cmds[-1]['t'] = t
            continue
        cmds.append({'t': t, 't0': t, 'kinds': {kind}, 'body': body})
    if not cmds:
        print('no commands found')
        return
    n = len(cmds)
    model = sum('model' in c['kinds'] for c in cmds)
    free = n - model
    by = collections.Counter(re.sub(r"[\d'(:].*", '', c['body']).strip()
                             for c in cmds if 'model' not in c['kinds'])
    print(f"window   {cmds[0]['t0']:%m-%d %H:%M} -> {cmds[-1]['t']:%m-%d %H:%M}")
    print(f'commands {n}')
    print(f'free     {free}  ({100 * free / n:.0f}%)')
    print(f'model    {model}  ({100 * model / n:.0f}%)')
    for k, v in by.most_common():
        print(f'  {v:4}  {k}')


def curator(path):
    done = collections.Counter()
    kinds = collections.Counter()
    lat = []
    with open(path, errors='replace') as f:
        for line in f:
            m = re.search(r'rung=(\d) model=(\S+)', line)
            if m and 'ack=silent' not in line:
                done[(m.group(1), m.group(2))] += 1
                k = re.search(r"-> \{'kind': '(\w+)'", line)
                if k:
                    kinds[k.group(1)] += 1
                e = re.search(r'elapsed=([\d.]+)s', line)
                if e and m.group(1) == '1' and m.group(2) != 'heuristic':
                    lat.append(float(e.group(1)))
            if 'rung1 failed' in line:
                done[('1', 'FAILED')] += 1
    for (rung, model), v in sorted(done.items()):
        print(f'rung {rung}  {model:28} {v}')
    print('rung-1 kinds', dict(kinds))
    if lat:
        lat.sort()
        print(f'rung-1 latency  n={len(lat)}  median={lat[len(lat) // 2]:.2f}s  '
              f'p90={lat[int(len(lat) * 0.9)]:.2f}s')


if __name__ == '__main__':
    year = dt.date.today().year
    voice(sys.argv[1], year)
    if len(sys.argv) > 2:
        print()
        curator(sys.argv[2])
