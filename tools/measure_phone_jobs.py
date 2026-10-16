#!/usr/bin/env python3
"""Count phone-miss worker jobs from a voice daemon log.

    python3 tools/measure_phone_jobs.py voice-input.log [--year 2026]
                                        [--intercepts confirm_pending,approve_pending,install_build]
                                        [--since 10-10] [--until 10-15]

Reads only the two lines router/phone_jobs.py (and the production daemon) log per job:

    MM-DD HH:MM:SS phone claude <id>: ... in the background        job started
    MM-DD HH:MM:SS phone claude <id>: rc=0 4.6s tools=['car']      job finished

Words are never read: the start line of the production log carries the request text, and this
script looks only at whether it ends with "in the background". Prints the window, jobs started and
finished, how many were answered by a regex intercept (the tools named in --intercepts: a spoken
"go ahead" / "confirm" matched without a model) versus a model session, and for model sessions the
tool histogram and the median / p90 of the worker's own seconds (n shown).

It cannot tell a real request from a test. If you ran scripted tests through the same endpoint,
they are in the count; say so when you quote it.
"""
import argparse
import collections
import re

LINE = re.compile(r'^(\d\d-\d\d) (\d\d:\d\d:\d\d) phone claude ([0-9a-f]{12}): (.*)$')
DONE = re.compile(r'^rc=(\S+) (\S+)s tools=(\[.*?\])')


def parse(path):
    start, done = {}, {}
    for raw in open(path, errors='replace'):
        m = LINE.match(raw.rstrip('\n'))
        if not m:
            continue
        day, hms, jid, rest = m.groups()
        if rest.rstrip().endswith('in the background'):
            start[jid] = (day, hms)
            continue
        d = DONE.match(rest)
        if d:
            tools = [t.strip().strip("'\"") for t in d.group(3)[1:-1].split(',') if t.strip()]
            done[jid] = {'day': day, 'rc': d.group(1), 'secs': float(d.group(2)) if d.group(2) != 'None' else None,
                         'tools': tools}
    return start, done


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--year', type=int, default=0)
    ap.add_argument('--intercepts', default='confirm_pending,approve_pending,install_build')
    ap.add_argument('--since', default='00-00')
    ap.add_argument('--until', default='99-99')
    a = ap.parse_args()
    start, done = parse(a.log)
    start = {j: v for j, v in start.items() if a.since <= v[0] <= a.until}
    done = {j: v for j, v in done.items() if j in start}
    if not start:
        print('no phone jobs found')
        return
    y = f'{a.year}-' if a.year else ''
    first = min(start.values())
    last = max(start.values())
    print(f'window   {y}{first[0]} {first[1]} -> {y}{last[0]} {last[1]}')
    print(f'started  {len(start)}   finished {len(done)}   rc: {dict(collections.Counter(v["rc"] for v in done.values()))}')
    inter = set(a.intercepts.split(','))
    regex = [j for j, v in done.items() if v['tools'] and set(v['tools']) <= inter]
    model = [v for j, v in done.items() if j not in regex]
    print(f'answered by a regex intercept (no model): {len(regex)}')
    print(f'model sessions: {len(model)}  (with a tool call {sum(1 for v in model if v["tools"])}, '
          f'none {sum(1 for v in model if not v["tools"])})')
    by_day = collections.Counter(v['day'] for v in model)
    print('model sessions by day:', dict(sorted(by_day.items())))
    tools = collections.Counter(t for v in model for t in v['tools'])
    print('tools:', dict(tools.most_common()))
    secs = sorted(v['secs'] for v in model if v['secs'] is not None)
    if secs:
        print(f'seconds  n={len(secs)}  median={pct(secs, 0.5):.1f}  p90={pct(secs, 0.9):.1f}  max={secs[-1]:.1f}')


if __name__ == '__main__':
    main()
