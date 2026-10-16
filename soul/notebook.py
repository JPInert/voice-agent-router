"""The files: what the soul writes and what it keeps as truth. Plain, append-only, hand-editable.

    profile.md         who the person is: hand-edited, injected into every reply
    persona.md         who the assistant is: hand-edited, injected into every reply
    events-DATE.jsonl  every heard / said / routed / miss line from the voice front end
    thoughts.md        its own notes, one line each:  - MM-DD HH:MM (source) text
    ideas.md           things it wants to build or try, same format
    memory.jsonl       facts the person asked it to keep (or approved), and `forget` records
    diary/DATE.md      the nightly summary of the day
    verdicts.jsonl     every screen verdict (addressed or not, gap, thought): the nightly's gap input
    soul.log           its own log

None of it belongs in git: it is a diary of a household. The directory is `Config.data_dir`.
"""
import glob
import json
import os
import time

from .text import norm, slurp, words

PROFILE_SEED = """# Profile: what the assistant knows about {user}
(Edit freely. Every reply reads this. One fact per line starting with "- ".)

"""

PERSONA_SEED = """# Who you are (edit freely; every reply reads this)
- You are the household's voice assistant, with a person's warmth: calm, capable, dry wit, loyal, never fussy.
- You remember. You bring up what matters from before the way someone who has been around a while
  would, and never recite your notes.
- Short. One or two sentences. You would rather do than explain.
- Honest about what you cannot do yet.
"""


def stamp(ts=None):
    return time.strftime('%m-%d %H:%M', time.localtime(ts or time.time()))


class Notebook:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg.dir

    def p(self, name):
        return os.path.join(self.dir, name)

    # --- setup / logging ---------------------------------------------------------------
    def ensure(self):
        os.makedirs(self.p('diary'), exist_ok=True)
        for name, seed in (('profile.md', PROFILE_SEED.format(user=self.cfg.user_name)), ('persona.md', PERSONA_SEED),
                           ('thoughts.md', '# What I have been thinking\n\n'),
                           ('ideas.md', '# Things I want to build or try\n\n')):
            if not os.path.exists(self.p(name)):
                with open(self.p(name), 'w') as f:
                    f.write(seed)

    def log(self, *a):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.p('soul.log'), 'a') as f:
                f.write(time.strftime('%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a) + '\n')
        except OSError:
            pass

    def append_jsonl(self, name, rec):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.p(name), 'a') as f:
                f.write(json.dumps(rec) + '\n')
        except OSError as e:
            self.log('write failed', name, e)

    def read_jsonl(self, name, since=0):
        out = []
        try:
            with open(self.p(name)) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get('ts', 0) >= since:
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def read(self, name, cap=None, default=''):
        try:
            t = slurp(self.p(name))
        except OSError:
            return default
        return t[:cap] if cap else t

    # --- the event stream ------------------------------------------------------------------
    def append_event(self, ev):
        day = time.strftime('%Y-%m-%d', time.localtime(ev.get('ts') or time.time()))
        self.append_jsonl(f'events-{day}.jsonl', ev)

    def events_for(self, day):
        return self.read_jsonl(f'events-{day}.jsonl')

    # --- thoughts and ideas ----------------------------------------------------------------
    def think(self, text, source='reply'):
        """Its own notes. Cheap and frequent on purpose."""
        text = norm(text)
        if not text:
            return
        self.ensure()
        with open(self.p('thoughts.md'), 'a') as f:
            f.write(f'- {stamp()} ({source}) {text}\n')

    def idea(self, text, source='gap'):
        text = norm(text)
        if not text:
            return
        self.ensure()
        have = self.read('ideas.md').lower()
        if text.lower()[:60] in have:       # the same gap offered twice is one idea
            return
        with open(self.p('ideas.md'), 'a') as f:
            f.write(f'- {stamp()} ({source}) {text}\n')

    def tail_lines(self, name, n):
        try:
            lines = [l for l in slurp(self.p(name)).splitlines() if l.startswith('- ')]
        except OSError:
            return []
        return lines[-n:]

    # --- facts the person asked it to keep -------------------------------------------------------
    def remember(self, fact, source='spoken'):
        fact = norm(fact).strip(' .')
        self.append_jsonl('memory.jsonl', {'ts': round(time.time(), 1), 'fact': fact, 'source': source})
        self.log('remember', fact)
        return fact

    def memories(self):
        """Live facts, oldest first (a 'forget' record removes the matching facts said before it)."""
        live = []
        for r in self.read_jsonl('memory.jsonl'):
            if r.get('forget'):
                ws = set(words(r['forget']))
                live = [m for m in live if not (ws and ws <= set(words(m['fact'])))]
            elif r.get('fact'):
                live.append(r)
        return live

    def forget(self, what):
        ws = set(words(what))
        hit = [m for m in self.memories() if ws and ws <= set(words(m['fact']))]
        if hit:
            self.append_jsonl('memory.jsonl', {'ts': round(time.time(), 1), 'forget': what})
        self.log('forget', what, len(hit))
        return len(hit)

    # --- diary -------------------------------------------------------------------------------------
    def diary_path(self, day):
        return self.p(os.path.join('diary', f'{day}.md'))

    def diaries(self, n=2):
        return sorted(glob.glob(self.p(os.path.join('diary', '*.md'))))[-n:]

    def write_diary(self, day, text):
        os.makedirs(self.p('diary'), exist_ok=True)
        with open(self.diary_path(day), 'w') as f:
            f.write(f'# {day}\n\n{text.strip()}\n')
