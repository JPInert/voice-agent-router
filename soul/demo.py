"""Load the synthetic household (fixtures/household.json) into a data directory, and show recall on it.

`python3 -m soul demo` needs no API key and no network: it builds an index over a few invented days and
runs a handful of searches, so you can see keyword + meaning recall working. With the embedding model
installed the "meaning" half is live; without it you get keyword-only and the demo says so.
"""
import datetime as dt
import json
import os
import tempfile
import time

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'household.json')


def load_fixture(path=FIXTURE):
    with open(path) as f:
        return json.load(f)


def _ts(day, hhmm):
    h, m = map(int, hhmm.split(':'))
    return time.mktime((day.year, day.month, day.day, h, m, 0, 0, 0, -1))


def materialise(dest, today=None, fx=None):
    """Write the fixture as a data directory (events, notes, memory, diary, profile). Returns the fixture."""
    fx = fx or load_fixture()
    today = today or dt.date.today()
    os.makedirs(os.path.join(dest, 'diary'), exist_ok=True)
    with open(os.path.join(dest, 'profile.md'), 'w') as f:
        f.write(f"# Profile: {fx['user_name']}\n\n" + '\n'.join(fx['profile']) + '\n')
    for off, evs in fx['days'].items():
        day = today + dt.timedelta(days=int(off))
        with open(os.path.join(dest, f'events-{day.isoformat()}.jsonl'), 'w') as f:
            for e in evs:
                f.write(json.dumps({'ts': _ts(day, e['t']), 'kind': e['kind'], 'text': e['text'],
                                    'what': e.get('what'), 'why': e.get('why')}) + '\n')
    for fname, key in (('thoughts.md', 'thoughts'), ('ideas.md', 'ideas')):
        with open(os.path.join(dest, fname), 'w') as f:
            f.write('# notes\n\n')
            for n in fx[key]:
                d = today - dt.timedelta(days=n['days_ago'])
                f.write(f"- {d.strftime('%m-%d')} {n['time']} ({n['source']}) {n['text']}\n")
    with open(os.path.join(dest, 'memory.jsonl'), 'w') as f:
        for m in fx['memory']:
            d = today - dt.timedelta(days=m['days_ago'])
            f.write(json.dumps({'ts': _ts(d, '12:00'), 'fact': m['fact'], 'source': 'spoken'}) + '\n')
    for off, text in fx['diary'].items():
        day = today + dt.timedelta(days=int(off))
        with open(os.path.join(dest, 'diary', f'{day.isoformat()}.md'), 'w') as f:
            f.write(f'# {day.isoformat()}\n\n{text}\n')
    return fx


QUERIES = [
    ('Mochi vet appointment', 'exact words: keyword search finds it'),
    ('when does the animal see the doctor', 'shares no word with "vet": needs the meaning model'),
    ('what did I ask about timers', 'the missed timer requests'),
]


def run(out=print):
    from .config import Config
    from .core import Soul
    tmp = tempfile.mkdtemp(prefix='soul-demo-')
    cfg = Config(data_dir=tmp, model_dir=Config.load().models)      # the model lives where `get-model` put it
    s = Soul(cfg)
    materialise(tmp)
    mode = 'keyword + meaning (bge-small found)' if s.embedder else 'keyword only (model not installed: run `python3 -m soul get-model`)'
    out(f'synthetic household in {tmp}\nsearch mode: {mode}\n')
    with s.store.session() as c:
        s.store.sync(c)
        s.store.embed_missing(c)
        out('index: ' + json.dumps(s.store.stats(c)) + '\n')
    for q, why in QUERIES:
        out(f'> {q}   ({why})')
        for r in s.search(q, k=3):
            out(f"    {r['day'][5:]} {r['kind']:8} {r['score']:.4f}  {r['text'][:90]}")
        out('')
    return tmp
