"""Rare-speak-first: when may the assistant say something nobody asked for?

The rule it was built under: it never talks unprompted, except the one thing it is allowed to say
rarely. The nightly job writes a few `shares` (a connection it noticed, a thought) and `questions`
(things it wonders about the person). This module decides when one may be spoken, and it is strict:

  * inside `share_hours` only (never at night or first thing);
  * only while a conversation is warm (the person spoke to it in the last `share_after_talk_s`);
  * at most a per-day quota, drawn from `share_per_day` and seeded by the calendar date, so a
    restart does not re-roll it;
  * at least `share_gap_s` apart;
  * only when the optional `presence()` hook says someone is home;
  * shares and questions take turns, at most one question a day.

`share_due()` only decides; the host speaks the line and then calls `share_said()`, which writes the
ledger the next decision reads.
"""
import random
import time


def quota(cfg, now=None):
    """Today's cap, stable across restarts: a RNG seeded by the date."""
    day = time.strftime('%Y-%m-%d', time.localtime(now or time.time()))
    return random.Random(day).randint(*cfg.share_per_day)


def share_due(s, last_talk_ts=None, now=None):
    """(share_id, text) the host may speak now, or None. Questions come back with the question prefix."""
    cfg = s.cfg
    now = now or time.time()
    h = time.localtime(now).tm_hour
    if not (cfg.share_hours[0] <= h < cfg.share_hours[1]):
        return None
    if not last_talk_ts or now - last_talk_ts > cfg.share_after_talk_s:
        return None
    if s.presence is not None and not s.presence():
        return None
    try:
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        if s.store.spoke_since(midnight) >= quota(cfg, now):
            return None
        if s.store.spoke_since(now - cfg.share_gap_s):
            return None
        row = s.store.waiting_share(now=now)
    except Exception as e:
        s.nb.log('share_due failed', e.__class__.__name__, e)
        return None
    if not row:
        return None
    return row['id'], (cfg.question_prefix if row.get('kind') == 'question' else '') + row['text']


def share_said(s, share_id, text):
    """Record that the host spoke it: it counts against today's quota and the gap."""
    s.store.note_spoke(text, share_id)
    pre = s.cfg.question_prefix
    if text.startswith(pre):
        s.nb.think(f'I asked them: {text[len(pre):]}', 'question')
    else:
        s.nb.think(f'I told them: {text}', 'share')
