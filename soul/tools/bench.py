"""Time the memory index on a SYNTHETIC corpus: sync, embed, then warm searches.

    python3 -m soul.tools.bench [N]        (default 2000 docs; needs the embedding model, see README)

Nothing private is read: the corpus is generated from fixed word lists with a fixed seed. Prints the
machine facts that matter next to the numbers, so a number is never quoted without its conditions.
"""
import json
import os
import platform
import random
import statistics
import sys
import tempfile
import time

from soul import Config, Soul

SUBJECTS = ['the kitchen lights', 'the thermostat', 'some folk music', 'a podcast about gardens', 'the weather tomorrow',
            'a timer for the bread', 'the front door camera', 'the living room speaker', 'tomorrow morning alarm',
            'the news headlines', 'a recipe for lentil soup', 'the balcony plants', 'a playlist for running']
VERBS = ['Turn on', 'Turn off', 'Play', 'Pause', 'What is', 'Set', 'Remind me about', 'Show me', 'Tell me about', 'Lower', 'Raise']


def corpus(n, seed=0):
    r = random.Random(seed)
    t0 = time.time() - 30 * 86400
    return [{'ts': t0 + i * (30 * 86400 / n), 'kind': r.choice(['heard', 'heard', 'said', 'routed']),
             'text': f'{r.choice(VERBS)} {r.choice(SUBJECTS)} number {i}.'} for i in range(n)]


def main(n=2000):
    tmp = tempfile.mkdtemp(prefix='soul-bench-')
    s = Soul(Config(data_dir=tmp, model_dir=Config.load().models))    # the model lives where `get-model` put it
    if s.embedder is None:
        sys.exit(f'embedding model not found in {s.cfg.models}: run `python3 -m soul get-model` first')
    rows = corpus(n)
    day = time.strftime('%Y-%m-%d')
    with open(os.path.join(tmp, f'events-{day}.jsonl'), 'w') as f:
        f.writelines(json.dumps(r) + '\n' for r in rows)
    with s.store.session() as c:
        t = time.time()
        synced = s.store.sync(c)
        t_sync = time.time() - t
        t = time.time()
        embedded = s.store.embed_missing(c)
        t_embed = time.time() - t
    queries = [f'{v.lower()} {sub}' for v in VERBS[:5] for sub in SUBJECTS[:10]]
    t = time.time()
    s.search(queries[0])
    t_first = time.time() - t
    lat = []
    for q in queries:
        t = time.time()
        s.search(q, k=8)
        lat.append(time.time() - t)
    lat.sort()
    print(json.dumps({
        'docs': synced, 'vectors': embedded, 'sync_s': round(t_sync, 2), 'embed_all_s': round(t_embed, 1),
        'first_search_s': round(t_first, 3), 'warm_searches': len(lat),
        'warm_median_ms': round(statistics.median(lat) * 1000, 1), 'warm_p95_ms': round(lat[int(len(lat) * 0.95) - 1] * 1000, 1),
        'db_mb': round(os.path.getsize(os.path.join(tmp, 'soul.db')) / 1e6, 2),
        'embed_threads': s.cfg.embed_threads, 'cpu': platform.processor() or platform.machine(),
        'cores': os.cpu_count(), 'python': platform.python_version(), 'date': time.strftime('%Y-%m-%d'),
    }, indent=1))


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
