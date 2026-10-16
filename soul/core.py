"""`Soul`: one object that ties config, files, index and model client together.

    from soul import Soul, Config
    s = Soul(Config.load('soul.toml'))        # the model client is built on first use; the embedding model loads on first search

    s.observe({'ts': t, 'kind': 'heard', 'text': '...'})     # feed it the voice front end's events
    v = s.screen('the line that fell through', stage='unclear', fallback='Sorry, I missed that.')
    say(v.speak)                                             # '' = stay silent
    line = s.share_due(last_talk_ts=t)                       # (id, text) or None; rare on purpose
    if line: say(line[1]); s.share_said(*line)

Event schema (what the voice front end hands `observe`):
    {"ts": 1767225600.0, "kind": "heard|said|routed|miss", "text": "...", "what": "...", "why": "...", "source": "..."}
"""
import collections
import time
from concurrent.futures import ThreadPoolExecutor

from . import embed as _embed
from . import facts, nightly, policy, recall, screen
from .config import Config
from .notebook import Notebook
from .store import Store


class Recent:
    """The last few minutes of what the house heard, said and did: the screen's short-term memory."""

    def __init__(self, window_s=300, maxlen=80):
        self.window_s = window_s
        self._d = collections.deque(maxlen=maxlen)

    @staticmethod
    def line(ev):
        k, t = ev.get('kind'), (ev.get('text') or '').strip()
        if k == 'heard' and t:
            return f'heard: {t}'
        if k == 'said' and t:
            return f'house said: {t}'
        if k == 'routed':
            return f"house did ({ev.get('what')}): {t}"
        if k == 'miss':
            return f"house missed ({ev.get('why')}): {t}"
        return None

    def add(self, ev):
        line = self.line(ev)
        if line:
            self._d.append((ev.get('ts') or time.time(), line[:200]))

    def add_raw(self, line, ts=None):
        """A pre-formatted line ('heard: ...'), for replaying a scenario."""
        self._d.append((ts or time.time(), line[:200]))

    def lines(self, now=None):
        now = now or time.time()
        return [f"{time.strftime('%H:%M:%S', time.localtime(ts))} {l}"
                for ts, l in list(self._d) if now - ts <= self.window_s][-25:]


class Soul:
    def __init__(self, cfg=None, client=None, embedder=None, presence=None):
        """client     anything with complete(system, user, model, max_tokens) -> str; default AnthropicClient.
        embedder   None = use the local bge-small model if it is installed, else keyword-only search;
                   False = never use one; or any object with dim / embed / embed_query.
        presence   optional callable -> bool; False means nobody is home and it must not volunteer anything."""
        self.cfg = cfg or Config.load()
        self.nb = Notebook(self.cfg)
        self._client = client
        if embedder is None and _embed.installed(self.cfg.models):
            embedder = _embed.OnnxEmbedder(self.cfg.models, self.cfg.embed_threads)
        self.embedder = embedder or None
        self.store = Store(self.cfg, self.embedder, log=self.nb.log)
        self.presence = presence
        self.recent = Recent(self.cfg.recent_window_s)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='soul')

    # --- model plumbing --------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            from .llm import AnthropicClient
            self._client = AnthropicClient()
        return self._client

    def run_with_timeout(self, fn, timeout_s):
        """Run fn() on a worker thread; TimeoutError if it takes longer (the thread is left to finish)."""
        return self._pool.submit(fn).result(timeout=timeout_s)

    def call(self, system, user, model, timeout_s, max_tokens=600):
        return self.run_with_timeout(lambda: self.client.complete(system, user, model, max_tokens), timeout_s)

    # --- the loop ----------------------------------------------------------------------------
    def observe(self, ev):
        """One event from the voice front end: remembered for the next minutes AND appended to today's file."""
        self.recent.add(ev)
        if ev.get('kind') in ('heard', 'said', 'routed', 'miss'):
            self.nb.append_event({k: ev.get(k) for k in ('ts', 'kind', 'text', 'what', 'why', 'source')})

    def screen(self, text, stage='unclear', fallback='', now=None):
        return screen.screen(self, text, stage, fallback, now)

    def context(self, query=None, now=None):
        return recall.context_block(self, now, query)

    def search(self, q, **kw):
        return recall.search(self, q, **kw)

    def recall_material(self, question, **kw):
        return recall.recall_material(self, question, **kw)

    def answer(self, question):
        return recall.answer(self, question)

    def nightly(self, day=None, force=False):
        return nightly.nightly(self, day, force)

    def share_due(self, last_talk_ts=None, now=None):
        return policy.share_due(self, last_talk_ts, now)

    def share_said(self, share_id, text):
        return policy.share_said(self, share_id, text)

    # --- inferred facts: queue, approve, reject -------------------------------------------------
    def pending(self):
        return self.store.pending()

    def approve(self, which):
        return facts.approve(self, which)

    def reject(self, which):
        return facts.reject(self, which)

    def learn(self, ask, limit=3):
        return facts.learn(self, ask, limit)
