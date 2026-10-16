"""Inferred facts wait for a yes.

The nightly job may notice something durable ("they cook for two on weeknights"). It is NEVER saved on
the model's say-so: it goes into the `pending` queue, and only `approve()` writes it to memory.jsonl,
where it starts colouring every later reply. A fact that was rejected is shown to the next nightly as
"said NO, never propose it again", and a paraphrase of anything known, queued or rejected is dropped
before it is queued (cosine >= cfg.fact_dup_sim).

Everything here is words in, rows out; the host decides how to ask (terminal, spoken yes/no, app).
"""


def _ids(s, which):
    """'all' or a numeric pending id; anything else is a ValueError with a plain message."""
    which = str(which).strip()
    if which == 'all':
        return [p['id'] for p in s.store.pending()]
    if not which.isdigit():
        raise ValueError(f"expected a pending id (a number, see `pending`) or 'all', got {which!r}")
    return [int(which)]


def approve(s, which):
    """'all' or a pending id -> a real memory (memory.jsonl, source=nightly). Returns the facts kept."""
    out = []
    for i in _ids(s, which):
        row = s.store.decide(i, True)
        if row:
            s.nb.remember(row['fact'], 'nightly')
            out.append(row['fact'])
    return out


def reject(s, which):
    return [r['fact'] for r in (s.store.decide(i, False) for i in _ids(s, which)) if r]


def learn(s, ask, limit=3):
    """Walk up to `limit` queued facts, asking `ask(question) -> True | False | None`.
    True keeps it, False drops it, None (silence, "what?", a TV) stops the round and leaves the
    rest queued. Returns (kept, dropped, left)."""
    kept = dropped = 0
    for i, p in enumerate(s.store.pending()[:limit]):
        q = ('Here is one. ' if i == 0 else 'And: ') + p['fact'].rstrip('.') + '. Shall I keep that?'
        ans = ask(q)
        if ans is True:
            approve(s, p['id'])
            kept += 1
        elif ans is False:
            reject(s, p['id'])
            dropped += 1
        else:
            break
    return kept, dropped, len(s.store.pending())
