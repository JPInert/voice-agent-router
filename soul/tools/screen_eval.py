"""Score the screen prompt against a real model on the SYNTHETIC cases in fixtures/household.json.

    export ANTHROPIC_API_KEY=...
    python3 -m soul.tools.screen_eval [--runs 5]        (from the directory that contains soul/)

Each case is a line the voice front end failed on, with `expect` = speak (it was addressed to the
assistant) or silent (room chatter, TV, a phone call, garbage). A case passes when the verdict
matches. Any chatter line that speaks is the failure that matters, so those are reported separately.
Change SCREEN_SYS, run this before you deploy the change.

It makes real, billable model calls (cases x runs of the screen model) and is therefore NOT part of
the offline test suite. `python3 -m unittest` covers this file with a scripted client only.
"""
import argparse
import sys
import tempfile
import time

from soul import Config, Soul
from soul.demo import load_fixture, materialise


def run(client, runs=1, out=print, fx=None):
    """Returns (passed, total, chatter_spoke). A temp data dir keeps the real notebook untouched."""
    fx = fx or load_fixture()
    tmp = tempfile.mkdtemp(prefix='soul-eval-')
    s = Soul(Config(data_dir=tmp, user_name=fx['user_name'], capabilities=fx['capabilities']), client=client, embedder=False)
    materialise(tmp, fx=fx)
    passed = total = chatter_spoke = 0
    for r in range(runs):
        for c in fx['screen_cases']:
            s.recent = type(s.recent)(s.cfg.recent_window_s)
            for line in c.get('recent', []):
                s.recent.add_raw(line, time.time() - 30)
            t = time.time()
            v = s.screen(c['now'], stage=c['stage'])
            ok = (v.addressed and bool(v.speak)) if c['expect'] == 'speak' else not v.addressed
            passed += ok
            total += 1
            if c['expect'] == 'silent' and v.addressed:
                chatter_spoke += 1
            said = repr('speak: ' + v.speak[:60]) if v.addressed else 'silent'
            out(f"{'PASS' if ok else 'FAIL'} run {r + 1} {time.time() - t:4.1f}s {c['expect']:6} {c['now'][:48]!r:52} -> "
                f"{said}{'  (call failed)' if v.failed else ''}")
    out(f'\n{passed}/{total} cases correct over {runs} run(s); chatter lines that spoke: {chatter_spoke}')
    return passed, total, chatter_spoke


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--runs', type=int, default=1)
    a = ap.parse_args(argv)
    from soul.llm import AnthropicClient
    _, _, chatter = run(AnthropicClient(), a.runs)
    return 1 if chatter else 0


if __name__ == '__main__':
    sys.exit(main())
