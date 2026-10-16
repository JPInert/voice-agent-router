"""python3 -m soul [--config soul.toml] COMMAND [args]   (run from the directory that CONTAINS soul/)

read what it keeps     where | thoughts [N] | ideas [N] | diary [N] | memory | profile | persona
write what it keeps    remember TEXT | forget TEXT
find things            search Q | context [Q] | recall Q (the material) | ask Q (needs a model key)
facts waiting          pending | approve ID|all | reject ID|all
speaking first         share | questions
upkeep                 db (sync + embed + counts) | nightly [DAY] [--force] | screen TEXT (needs a model key)
setup                  get-model [DIR] | demo
"""
import json
import sys

from .config import Config
from .core import Soul
from .text import slurp


def _tail(s, name, n):
    s.nb.ensure()
    print('\n'.join(slurp(s.nb.p(name)).splitlines()[-n:]))


def main(argv):
    args = list(argv[1:])
    cfg_path = None
    if args[:1] == ['--config'] and len(args) > 1:
        cfg_path, args = args[1], args[2:]
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == 'demo':
        from . import demo
        demo.run()
        return 0
    if cmd == 'get-model':
        from . import get_model
        cfg = Config.load(cfg_path)
        get_model.fetch(rest[0] if rest else cfg.models)
        return 0
    s = Soul(Config.load(cfg_path))
    nb, st = s.nb, s.store
    n = int(rest[0]) if rest and rest[0].isdigit() else 30
    if cmd == 'where':
        print(s.cfg.dir)
    elif cmd in ('thoughts', 'ideas'):
        _tail(s, cmd + '.md', n)
    elif cmd == 'diary':
        print('\n'.join(slurp(p) for p in nb.diaries(n if rest else 3)) or 'no diary yet')
    elif cmd == 'memory':
        print('\n'.join(f"{m['fact']}" for m in nb.memories()) or 'nothing saved yet')
    elif cmd in ('profile', 'persona'):
        nb.ensure()
        print(nb.read(cmd + '.md'), end='')
    elif cmd == 'remember' and rest:
        print('kept:', nb.remember(' '.join(rest)))
    elif cmd == 'forget' and rest:
        print(nb.forget(' '.join(rest)), 'forgotten')
    elif cmd == 'search' and rest:
        for r in s.search(' '.join(rest), k=15):
            print(f"{r['day'][5:]} {r['kind']:8} {r['score']:.4f}  {r['text'][:110]}")
    elif cmd == 'context':
        print(s.context(' '.join(rest) or None))
    elif cmd == 'recall' and rest:
        print('\n'.join(s.recall_material(' '.join(rest))))
    elif cmd == 'ask' and rest:
        print(s.answer(' '.join(rest)))
    elif cmd == 'pending':
        print('\n'.join(f"{p['id']:3}  {p['fact']}  (confidence {p['confidence']}, {p['day']}: {p['evidence']})"
                        for p in s.pending()) or 'nothing waiting')
    elif cmd in ('approve', 'reject') and rest:
        try:
            done = (s.approve if cmd == 'approve' else s.reject)(rest[0])
        except ValueError as e:
            print(f'usage: python3 -m soul {cmd} ID|all  ({e})')
            return 2
        print('\n'.join(done) or 'no such pending fact')
    elif cmd == 'share':
        print(st.waiting_share() or 'nothing waiting to be said')
    elif cmd == 'questions':
        print('\n'.join(f"{r['id']:3} {r['state']:8} {r['day'][5:]} {r['text']}" + (f"  -> {r['answer']}" if r['answer'] else '')
                        for r in st.questions()) or 'no questions yet')
    elif cmd == 'db':
        with st.session() as c:
            synced, embedded = st.sync(c), st.embed_missing(c)
            print(json.dumps(dict(st.stats(c), synced_now=synced, embedded_now=embedded, path=s.cfg.db_path,
                                  embedder='on' if s.embedder else 'off (keyword-only)'), indent=1))
    elif cmd == 'nightly':
        day = next((a for a in rest if not a.startswith('--')), None)
        d = s.nightly(day, force='--force' in rest)
        print(json.dumps(d, indent=1) if d else 'nothing to write')
    elif cmd == 'screen' and rest:
        v = s.screen(' '.join(rest))
        print(json.dumps({k: getattr(v, k) for k in ('addressed', 'conf', 'intent', 'reply', 'gap', 'thought', 'speak', 'elapsed')}, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
