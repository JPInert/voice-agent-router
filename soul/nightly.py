"""The nightly job: read yesterday, write the diary, keep a notebook, queue what it learned.

Shape of the job (run it from a timer, see deploy/):
  1. Gather the day: events, the screen's gap ledger, requests the person repeated.
  2. Gather memory from the index: what it already knows (confirmed, queued, rejected), older notes
     that MEAN the same as today's talk (FROM BEFORE), its still-open ideas, its own questions.
  3. ONE model call returning a JSON object: diary, thoughts, ideas, idea_updates, facts, shares,
     questions, question_updates.
  4. Apply it conservatively: facts are QUEUED for approval, never saved; an idea close is accepted only
     for an id the model was shown; a new idea that means the same as a known one is dropped; facts under
     the confidence floor are dropped.
  5. Write the diary file LAST. Its existence means "this night is done", so a run that dies halfway
     is redone by the next attempt. An unusable reply RAISES (exit 1), so a timer with
     Restart=on-failure retries instead of silently skipping the night.
"""
import datetime as dt
import re
import time

from .text import json_from, norm

NIGHTLY_SYS = """You are the inner voice of a home voice assistant (wake word "%(wake)s") that serves %(user)s. Once a day you look back over the day and write in your own notebook. Nobody reads this aloud; %(user)s may read it at a terminal. Write plainly, in the first person, like a curious companion who is getting to know them.

You get: their PROFILE, WHAT YOU ALREADY KNOW (facts they confirmed, ones still waiting for their yes, and ones they said NO to), the day's EVENTS (what they said to the house, what the house said and did, what it missed), the GAPS ledger (things they asked for that the house could not do), REPEATED REQUESTS, FROM BEFORE (your older diary days, thoughts and ideas that relate to today - build on them, notice what changed, don't repeat them), OPEN IDEAS (your earlier ideas, each with an id), and YOUR QUESTIONS (things you wondered about them: waiting to be asked, asked with what they said in the 15 min after, or answered). Use only what is there; never invent events.
The room may have a TV and other people: a line is THEIRS only when it is clearly addressed to the house or clearly about themselves.

Reply with ONLY one JSON object:
{"diary": "<3-6 sentences: what the day was like from the house's side>",
 "thoughts": ["<2-5 short observations or wonderings about %(user)s, their taste, or yourself - each one sentence; where FROM BEFORE relates, connect to it (\\"again\\", \\"for the first time since\\"); they may be read aloud, so plain spoken language>"],
 "ideas": ["<0-3 NEW concrete things to build or try, each one sentence, grounded in today, and not already in OPEN IDEAS>"],
 "idea_updates": [{"id": <OPEN IDEAS id>, "state": "done|dropped", "why": "<short: the events show it now works, or it no longer matters>"}],
 "facts": [{"fact": "<a durable fact about %(user)s or their household, third person, one sentence>", "confidence": <0.0-1.0>, "evidence": "<the HH:MM event line(s) it rests on>"}],
 "shares": ["<1-3 separate sentences you would genuinely like to say to %(user)s tomorrow if the moment is right - a connection you noticed, a note or a thought; each stands alone (they are spoken hours apart, best first); warm, never about something overheard from other people>"],
 "questions": ["<0-2 things you genuinely wonder about %(user)s and would like to ask them, each ONE short spoken question to them (\\"you\\"); only what WHAT YOU ALREADY KNOW does not answer and YOUR QUESTIONS does not already hold in other words; never about other people's private lives>"],
 "question_updates": [{"id": <YOUR QUESTIONS id>, "state": "answered|dropped", "answer": "<answered: what they said, in a few words; dropped: why - asked days ago and never answered, or no longer matters>"}]}

questions they answered: also put the durable part of their answer in facts (they still approve it).
facts: 0-3, only what EVENTS show and WHAT YOU ALREADY KNOW does not already say; %(user)s approves each one before you keep it, so be sparing and exact.

%(style)s"""

_HEARD = re.compile(r'^(\d\d):(\d\d) heard: (.*)$')


def day_material(s, day):
    """(event lines, gap rows) for one day. Lines read 'HH:MM kind(what): text'."""
    lines = []
    for e in s.nb.events_for(day):
        k, t = e.get('kind'), (e.get('text') or '')[:140]
        if k in ('heard', 'said', 'routed', 'miss') and t:
            what = f"({e.get('what')})" if k == 'routed' else ''
            lines.append(f"{time.strftime('%H:%M', time.localtime(e.get('ts', 0)))} {k}{what}: {t}")
    t0 = time.mktime(time.strptime(day, '%Y-%m-%d'))
    gaps = [r for r in s.nb.read_jsonl('verdicts.jsonl', t0) if r.get('ts', 0) < t0 + 86400 and r.get('gap')]
    return lines[-240:], gaps


def repeats_after_success(lines):
    """The same request twice within 2 minutes: the first answer was probably not what they wanted."""
    heard = [(int(m.group(1)) * 60 + int(m.group(2)), m.group(1) + ':' + m.group(2), norm(m.group(3)).lower().strip(' .!?'))
             for m in map(_HEARD.match, lines) if m]
    return [f'{t1}/{t2} "{a}" asked twice' for (m1, t1, a), (m2, t2, b) in zip(heard, heard[1:])
            if a and a == b and 0 <= m2 - m1 <= 2]


def _memory_sections(s, day, lines):
    """What the nightly reads from the index. Any failure leaves it working with empty sections."""
    nb, st = s.nb, s.store
    out = {'known': [f"- {m['fact']} (confirmed)" for m in nb.memories()], 'before': [], 'ideas': []}
    try:
        with st.session() as c:
            st.sync(c)
            st.embed_missing(c)
            out['known'] += [f"- {p['fact']} (waiting for their yes)" for p in st.pending(c=c)]
            out['known'] += [f"- {p['fact']} (they said NO - not true or not wanted; never propose it again)"
                             for p in st.pending('rejected', c=c)]
            heard = [m.group(3) for m in map(_HEARD.match, lines) if m][-40:]
            t0 = time.mktime(time.strptime(day, '%Y-%m-%d'))
            out['before'] = [f"- {r['day']} {r['kind']}: {r['text'][:300]}"
                             for r in st.related_to(heard, k=12, before=t0, c=c)]
            out['ideas'] = [f"- id {r['id']} ({r['day'][5:]}): {r['text']}" for r in st.open_ideas(c=c)]
    except Exception as e:
        nb.log('nightly memory failed', e.__class__.__name__, e)
    return out


def question_material(s, day):
    """Its questions: waiting, asked (with the person's next 15 minutes if asked that day), answered."""
    try:
        rows = s.store.questions()
    except Exception:
        return []
    t0 = time.mktime(time.strptime(day, '%Y-%m-%d'))
    evs = s.nb.events_for(day)
    drop_after = s.cfg.question_drop_days * 86400
    out = []
    for r in rows:
        if r['state'] == 'dropped' or (r['state'] == 'answered' and (r['ts'] or 0) < t0 - 30 * 86400):
            continue
        line = f"id {r['id']} {r['state']}: {r['text']}"
        if r['state'] == 'answered':
            line += f"  -> they said: {r['answer']}"
        elif r['state'] == 'asked' and r['ts']:
            after = [f"{time.strftime('%H:%M', time.localtime(e['ts']))} {e['kind']}: {(e.get('text') or '')[:100]}"
                     for e in evs if r['ts'] < (e.get('ts') or 0) <= r['ts'] + 900 and e.get('kind') in ('heard', 'said')]
            if after:
                line += f"\n   asked {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}; next 15 min: " + ' | '.join(after[:8])
            else:
                line += f"\n   asked {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}" + \
                        ('' if r['ts'] >= t0 else '; no answer that day') + \
                        ('; drop it now' if r['ts'] < t0 - drop_after else '')
        out.append(line)
    return out


def build_body(s, day, lines, gaps, reps, mem, q_material):
    return ('PROFILE:\n' + s.nb.read('profile.md', 1500) +
            '\n\nWHAT YOU ALREADY KNOW:\n' + ('\n'.join(mem['known']) or '(nothing yet)') +
            f'\n\nEVENTS {day}:\n' + '\n'.join(lines) +
            '\n\nGAPS:\n' + ('\n'.join(str(r['gap']) for r in gaps) or '(none)') +
            '\n\nREPEATED REQUESTS:\n' + ('\n'.join(reps) or '(none)') +
            '\n\nFROM BEFORE:\n' + ('\n'.join(mem['before']) or '(nothing)') +
            '\n\nOPEN IDEAS:\n' + ('\n'.join(mem['ideas']) or '(none)') +
            '\n\nYOUR QUESTIONS:\n' + ('\n'.join(q_material) or '(none yet)'))


def _ask_model(s, body, day):
    cfg = s.cfg
    system = NIGHTLY_SYS % {'wake': cfg.wake_word, 'user': cfg.user_name, 'style': cfg.spoken_style}
    raw, d = '', {}
    for attempt in range(2):            # a broken-JSON answer happens; one retry with a nudge
        sysp = system + ('' if not attempt else
                         '\n\nYour last answer was not valid JSON. Escape every double quote inside strings.')
        raw = s.client.complete(sysp, body, cfg.nightly_model, max_tokens=4096)
        d = json_from(raw) or {}
        if d:
            break
        s.nb.log('nightly', day, f'attempt {attempt + 1} unparseable:', (raw or '')[:200])
    return d, raw


def nightly(s, day=None, force=False):
    """Run the job for `day` (default yesterday). Returns the model's cleaned reply, or None when
    there was nothing to write or the day is already done. Raises RuntimeError on an unusable reply."""
    cfg, nb, st = s.cfg, s.nb, s.store
    nb.ensure()
    day = day or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    if nb.read(f'diary/{day}.md') and not force:
        nb.log('nightly', day, 'already written')      # a re-run must not double the thoughts
        return None
    lines, gaps = day_material(s, day)
    if not lines:
        nb.log('nightly', day, 'nothing heard')
        return None
    reps = repeats_after_success(lines)
    mem = _memory_sections(s, day, lines)
    q_material = question_material(s, day)
    d, raw = _ask_model(s, build_body(s, day, lines, gaps, reps, mem, q_material), day)
    if not d or not isinstance(d.get('diary'), str) or not d['diary'].strip():
        nb.log('nightly', day, 'unparseable or no diary:', (raw or '')[:200])
        raise RuntimeError(f'nightly {day}: no usable reply')
    for k in ('thoughts', 'ideas', 'idea_updates', 'facts', 'questions', 'question_updates'):
        v = d.get(k)
        d[k] = [x for x in v if x] if isinstance(v, list) else ([v] if isinstance(v, str) and v.strip() else [])
    d['shares'] = [x.strip() for x in d.get('shares') if isinstance(x, str) and x.strip()][:3] \
        if isinstance(d.get('shares'), list) else []

    for t in d['thoughts']:
        nb.think(str(t), f'reflection {day[5:]}')

    candidates = ([(str(i), f'reflection {day[5:]}') for i in d['ideas']] +
                  [(str(g['gap']), f'gap {day[5:]}') for g in gaps] +
                  [(f'Same request twice in a row: {r}. The first answer probably was not what they wanted.',
                    f'repeat {day[5:]}') for r in reps])
    kept = 0
    for text, src in candidates:
        hit = _known_idea(s, text)
        if hit:
            nb.log(f"idea already known ({hit['state']}, {hit['score']}): {text[:80]!r}")
            continue
        nb.idea(text, src)
        kept += 1

    shown = {int(x.split()[2]) for x in mem['ideas'] if x.split()[2].isdigit()}        # "- id N (...)"
    closed = 0
    for u in d['idea_updates']:
        if isinstance(u, dict) and str(u.get('id', '')).isdigit() and int(u['id']) in shown \
                and u.get('state') in ('done', 'dropped'):
            closed += st.set_idea(int(u['id']), u['state'], u.get('why') or '')

    proposed = 0
    for f in d['facts']:
        try:
            conf = float(f.get('confidence') or 0) if isinstance(f, dict) else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        if isinstance(f, dict) and f.get('fact') and conf >= cfg.fact_min_confidence:
            proposed += bool(st.propose(f['fact'], conf, f.get('evidence') or '', day))

    for i, t in enumerate(reversed(d['shares'])):          # best last = newest id = spoken first
        st.add_share(day, t, keep=i > 0)
    asked = 0
    for q in [q.strip() for q in d['questions'] if isinstance(q, str) and q.strip()][:2]:
        asked += bool(st.add_share(day, q, keep=True, kind='question'))
    q_shown = {int(x.split()[1]) for x in q_material if x.split()[1].isdigit()}         # "id N state: ..."
    for u in d['question_updates']:
        if isinstance(u, dict) and str(u.get('id', '')).isdigit() and int(u['id']) in q_shown \
                and u.get('state') in ('answered', 'dropped'):
            st.set_question(int(u['id']), u['state'], str(u.get('answer') or '')[:300])

    nb.write_diary(day, d['diary'])      # LAST: its existence means "night done"
    nb.log('nightly', day, f"diary ok, {len(d['thoughts'])} thoughts, {kept}/{len(candidates)} new ideas, "
           f"{closed} closed, {proposed} facts queued, shares={len(d['shares'])}, questions={asked}")
    return d


def _known_idea(s, text):
    try:
        return s.store.similar_idea(text)
    except Exception as e:
        s.nb.log('idea dedupe failed', e.__class__.__name__)
        return None
