#!/usr/bin/env python3
"""Type what you would say; see which tier claims it.

    python3 demo.py --dry-run      # no model calls: shows tier 0 / heuristic / "would go to rung 1"
    python3 demo.py                # real rungs 1-3 via the Claude Agent SDK (needs `claude` logged in)

Tier 0 actions are printed, not executed (there is no Home Assistant or Kodi here),
except `say`. Permission questions from rungs 2/3 are asked on this terminal.
"""
import argparse
import os
import sys

from router import tricks
from router.ladder import Router, heuristic
from router.snapshot import text as machine_snapshot
from router.voice_io import ConsoleVoice

HERE = os.path.dirname(os.path.abspath(__file__))


class _PrintHA:
    def call(self, domain, service, data):
        print(f'  [ha] {domain}.{service} {data}')

    def process(self, text):
        print(f'  [ha assist] {text!r}')
        return True, ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='never call a model')
    ap.add_argument('--tricks', default=os.path.join(HERE, 'examples', 'tricks.yaml'))
    a = ap.parse_args()

    voice = ConsoleVoice()
    loader = tricks.Loader(a.tricks)
    router = None if a.dry_run else Router(voice, snapshot=machine_snapshot)
    ctx = tricks.TrickCtx(say=voice.say, ha=_PrintHA(), log=lambda *x: None,
                          confirm=lambda p: (voice.ask(p, 'yesno') or {}).get('yes') is True,
                          curator=lambda t: print(f'  [curator] {t!r}'))
    print('say something (ctrl-d to quit)')
    for line in sys.stdin if not sys.stdin.isatty() else iter(lambda: input('> '), None):
        text = line.strip()
        if not text:
            continue
        t = tricks.match(text, loader.poll())
        if t:
            print(f'  tier 0: trick {t.name!r} (no model)')
            if any(k == 'shell' for k, _ in t.actions):
                print(f'  [shell] {[v for k, v in t.actions if k == "shell"]} (not run in the demo)')
                continue
            tricks.run(t, ctx)
            continue
        if a.dry_run:
            h = heuristic(text)
            print('  rung 1: heuristic -> device job (no model)' if h else
                  '  rung 1: would go to Haiku to classify')
            continue
        r = router.utterance(text)
        if r:
            voice.say(r)


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print()
