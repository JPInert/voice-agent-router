"""Fakes shared by the tests: a scripted model client and a hashing embedder. Nothing here is a model."""
import json
import os
import re
import tempfile
import time
import unittest
import zlib

import numpy as np

from soul import Config, Soul


class FakeClient:
    """Stands in for the model. `replies` is a list (popped in order, the last one repeats) or a function
    (system, user, model) -> str. Every call is recorded in `.calls`."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def complete(self, system, user, model, max_tokens=1024):
        self.calls.append({'system': system, 'user': user, 'model': model, 'max_tokens': max_tokens})
        if callable(self.replies):
            return self.replies(system, user, model)
        r = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


class HashEmbedder:
    """Bag-of-words hashing into 256 dims, with a crude 5-letter stem. Deterministic, offline, and
    good enough to make 'shares most of its words' behave like 'similar'. It does NOT know that a cat
    is a pet: that needs the real model (tests/test_embed_real.py)."""
    dim = 256

    def _one(self, text):
        v = np.zeros(self.dim, np.float32)
        for w in re.findall(r'[a-z0-9]+', text.lower()):
            if len(w) > 2:
                v[zlib.crc32(w[:5].encode()) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts):
        return np.stack([self._one(t) for t in texts]) if len(texts) else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text):
        return self._one(text)


class SoulCase(unittest.TestCase):
    """A fresh data directory per test; never anything outside it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='soul-test-')
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        os.environ.pop('SOUL_DB', None)

    def make(self, client=None, embedder=False, presence=None, **cfg):
        cfg.setdefault('data_dir', self.dir)
        return Soul(Config(**cfg), client=client, embedder=HashEmbedder() if embedder is True else embedder,
                    presence=presence)

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        return p

    def read(self, *parts):
        with open(os.path.join(self.dir, *parts)) as f:
            return f.read()

    def events(self, day, rows):
        self.write(f'events-{day}.jsonl', ''.join(json.dumps(r) + '\n' for r in rows))
