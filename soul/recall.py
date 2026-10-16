"""Reading memory back: the context every reply carries, and "do you remember ...?" answers.

Both go through Store.search (keyword + meaning, fused). If the embedding model is missing the search
is keyword-only and recall_material falls back to plain word overlap, so nothing here ever raises
because a model file is absent.
"""
import datetime as dt
import re
import time

from .notebook import stamp
from .text import norm, slurp, speakable, words

RECALL_SYS = """%(persona)s

%(user)s just asked you to remember something from before. Below is MEMORY: lines from your own records
(what they said, what you said and did, your diary, your own thoughts, facts they asked you to keep),
with dates. Answer the question in one or two spoken sentences, from MEMORY only, the way a friend who
was there would, with the day or time when it helps. If MEMORY does not hold the answer, say plainly
you don't remember that. No lists, no markdown, no code.

%(style)s"""

_KIND_LABEL = dict(heard='they said', said='you said', diary='diary', thought='you thought',
                   memory='they asked you to remember', idea='your idea')


def strip_wake(q, wake):
    """The wake word means nothing to a search."""
    return re.sub(rf'^\W*(?:hey\W+)?{re.escape(wake)}\b\W*', '', q or '', flags=re.I) or q


def search(s, q, **kw):
    return s.store.search(strip_wake(q, s.cfg.wake_word), **kw)


def context_block(s, now=None, query=None):
    """Everything a reply reads. With `query` (what the person just said) it also carries what the
    house remembers that MEANS the same: older talk, diary days, its own thoughts, saved facts."""
    now = now or time.time()
    nb = s.nb
    nb.ensure()
    out = []
    for head, name, cap in (('WHO YOU ARE', 'persona.md', 1000),
                            (f'ABOUT {s.cfg.user_name.upper()} (their profile)', 'profile.md', 1500)):
        t = nb.read(name, cap).strip()
        if t:
            out.append(f'{head}:\n{t}')
    mem = nb.memories()[-15:]
    if mem:
        out.append('THINGS THEY ASKED YOU TO REMEMBER:\n' + '\n'.join(f"- {m['fact']} ({stamp(m['ts'])})" for m in mem))
    for path in nb.diaries(2):
        day = path.rsplit('/', 1)[-1][:-3]
        out.append(f'DIARY {day}: ' + norm(' '.join(l for l in slurp(path).splitlines() if not l.startswith('# ')))[:500])
    th = nb.tail_lines('thoughts.md', 4)
    if th:
        out.append('YOUR RECENT THOUGHTS:\n' + '\n'.join(th))
    if query:
        try:
            rel = search(s, query, k=8, until=now - 300, min_sim=s.cfg.context_min_sim,
                         kinds=('heard', 'said', 'diary', 'thought', 'memory', 'idea'))
            if rel:
                out.append('FROM BEFORE (what you remember that relates to what they just said):\n' + '\n'.join(
                    f"- {r['day'][5:]} {_KIND_LABEL.get(r['kind'], r['kind'])}: {r['text'][:220]}" for r in rel))
        except Exception as e:
            nb.log('context search failed', e.__class__.__name__, e)
    return '\n\n'.join(out)


def _hits(s, question, days, today):
    """{(kind, normalised text) | ('diary', day): row}, or None when search itself failed."""
    try:
        since = time.mktime((today - dt.timedelta(days=days)).timetuple())
        rows = search(s, question, k=40, since=since, kinds=('heard', 'said', 'routed', 'diary', 'thought'))
    except Exception as e:
        s.nb.log('recall search failed', e.__class__.__name__, e)
        return None
    return {(('diary', r['day']) if r['kind'] == 'diary' else (r['kind'], r['text'].lower().strip(' .!?'))): r
            for r in rows}


def recall_material(s, question, days=30, limit=45, today=None):
    """What the model gets to answer 'do you remember ...': the matching lines from the event files
    and diaries (whole days when the person names one), plus saved facts and its own past thoughts."""
    nb = s.nb
    q = question.lower()
    today = today or dt.date.today()
    want = []
    if 'yesterday' in q or 'last night' in q:
        want.append((today - dt.timedelta(days=1)).isoformat())
    if 'today' in q or 'this morning' in q or 'tonight' in q:
        want.append(today.isoformat())
    ws = set(words(question)) - {'watch', 'watching', 'ask', 'asked', 'say', 'said', 'tell', 'told', 'play', 'played'}
    hits = _hits(s, question, days, today)       # None = search down: word overlap instead
    out = []
    for i in range(days - 1, -1, -1):             # oldest -> newest, so the tail is the latest
        day = (today - dt.timedelta(days=i)).isoformat()
        whole = day in want
        for e in nb.events_for(day):
            t = e.get('text') or ''
            if e.get('kind') not in ('heard', 'said', 'routed') or not t:
                continue
            match = ((e['kind'], norm(t).lower().strip(' .!?')) in hits) if hits is not None \
                else bool(ws and ws & set(words(t)))
            if whole or match:
                who = {'heard': 'they said', 'said': 'you said', 'routed': 'you did'}[e['kind']]
                out.append(f"{day[5:]} {time.strftime('%H:%M', time.localtime(e.get('ts', 0)))} {who}: {t[:140]}")
        body = nb.read(f'diary/{day}.md')
        if body and (whole or (('diary', day) in hits if hits is not None else bool(ws and ws & set(words(body))))):
            out.append(f'diary {day}: ' + norm(body)[:600])
    facts = [f"{stamp(m['ts'])} they told you to remember: {m['fact']}" for m in nb.memories()]
    if hits:                                       # its own past thoughts on the subject, not just events
        facts += [f"{h['day'][5:]} you thought: {h['text'][:200]}" for h in hits.values() if h['kind'] == 'thought'][:5]
    return facts + out[-limit:]


def answer(s, question):
    """'Do you remember ...?' -> one or two spoken sentences, from memory only. Words only: it acts on nothing."""
    cfg = s.cfg
    mat = recall_material(s, question)
    system = (RECALL_SYS % {'persona': s.nb.read('persona.md', 1000), 'user': cfg.user_name.capitalize(),
                            'style': cfg.spoken_style}).strip()
    try:
        reply = s.call(system, 'MEMORY:\n' + ('\n'.join(mat) or '(nothing)') + f'\n\nTHEY ASKED: {question}',
                       cfg.recall_model, cfg.recall_timeout_s)
    except Exception as e:
        s.nb.log('recall failed', e.__class__.__name__, e)
        return "I can't reach my memory right now."
    return speakable(norm(reply))
