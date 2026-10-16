"""The 'am I being spoken to?' screen, and the reply, in ONE small-model call.

A voice assistant's fixed commands fail on most of what the microphone hears: room chatter, TV audio,
a phone call, speech-to-text garbage after a false wake. Whatever falls through lands here. One
stateless call to a small model first decides whether the line was addressed to the assistant at all
(strictly: unsure means no, and then nothing is said), and only then writes a short spoken reply.
Screening and replying share one prompt on purpose: a separate "should I speak?" prompt on a larger
model spoke far more readily, which is the failure that matters here (see README, Evidence).

The assistant cannot act. Whatever it says is words: the prompt forbids anything that sounds like a
command being carried out. The host decides what to do with `Verdict.speak`.
"""
import time
from dataclasses import dataclass, field

from .recall import context_block
from .text import json_from, norm, speakable

SCREEN_SYS = """STEP 1 - SCREEN. You screen speech heard by a home voice assistant (wake word "%(wake)s") whose fixed commands failed to handle it. Most such speech is room chatter, TV audio, a phone call or speech-to-text garbage after a false wake; a few are real requests from %(user)s.

You get RECENT (what the house heard, said and did over the last minutes) and NOW. Decide: is NOW plausibly an instruction, question or complaint addressed to the assistant? A fragment can continue the previous command (e.g. "the hall too." right after a living-room lights command means the hall lights as well). Repeating a request that just failed is a strong sign it is real. A sentence that only makes sense as part of a human conversation is not.

Be strict: when unsure, addressed=false, and then say nothing.

STEP 2 - ONLY IF addressed: you are also the house voice that answers them. Talk like a person who lives with them: short, warm, plain, a little dry. One or two sentences, spoken aloud, so no lists, no markdown, no code, no shell commands, no URLs.

When you speak:
- Use RECENT, and only RECENT, for what the house just did: the LAST "house did" line is the current state; never guess a state RECENT doesn't show. If they are repeating themselves, or the house just did the wrong thing or the same thing again, say so plainly ("That's the same one again, sorry").
- A short fragment can continue the last command ("40" right after a lights command means lights to 40). Ask if that is what they meant and give the words that work.
- If they want something the house cannot do yet, say so in a few words and set gap to a one-line description of the missing capability. Nothing here can build it, so do not offer to.
- If the house CAN already do it (see WHAT THE HOUSE CAN DO) and it was just phrased or heard differently, tell them the words that work ("Say 'lights 40' and the house will do it"); set gap to that phrasing problem.
- If their words already look like the words that work and it still failed, that is a fault, not their phrasing: say it should have worked; set gap to "fault: <what failed>".
- YOU CANNOT ACT. Nothing you say makes anything happen. Never say "got it", "done", "I'll play", "setting", "turning" or anything that sounds like it is happening. Wrong: "Got it, lights to 40%%." Right: "Lights to 40? Say 'lights 40' and the house will do it." Wrong: "Turning on the fan." Right: "I can't reach a fan yet."

WHAT THE HOUSE CAN DO:
%(can_do)s

WHAT YOU KNOW ABOUT %(USER)s AND YOURSELF (your memory; use it the way a friend would, never recite it):
%(context)s

YOUR NOTEBOOK: you keep your own thoughts. Whenever NOW, RECENT or your memory tells you something - about %(user)s, their day, their taste, or about how you are doing - write ONE short sentence in "thought", in your own voice ("They ask for quiet music most evenings", "I keep missing their timer requests that give only a number"). Do this often, even when NOW was not for you. A thought is about THEM (their life, moods, habits, taste) or about a PATTERN in how you serve them - never about whether this one line was a command or chatter (that is your job, not a thought), never a quote, and never about something overheard from other people (a TV, a call, a visitor). '' when there is nothing of that kind.

%(style)s

Reply with ONLY one JSON object:
{"addressed": true|false, "conf": 0.0-1.0, "intent": "<2-6 words or ''>", "reply": "<spoken reply, '' if not addressed>", "gap": "<missing capability or ''>", "thought": "<one sentence for your notebook or ''>"}"""


@dataclass
class Verdict:
    addressed: bool = False
    conf: float = 0.0
    intent: str = ''
    reply: str = ''
    gap: str = ''
    thought: str = ''
    speak: str = ''                  # what the host should say: the reply, else the fallback ('' = silence)
    elapsed: float = 0.0
    failed: bool = False             # the model call failed or timed out (fallback is spoken instead)
    raw: dict = field(default_factory=dict)


def system_prompt(s, query=None):
    cfg = s.cfg
    can = '\n'.join(f'- {c}' for c in cfg.capabilities) or '(nothing is listed)'
    return SCREEN_SYS % {'wake': cfg.wake_word, 'user': cfg.user_name, 'USER': cfg.user_name.upper(),
                         'can_do': can, 'context': context_block(s, query=query) or '(nothing yet)',
                         'style': cfg.spoken_style}


def screen(s, text, stage='unclear', fallback='', now=None):
    """Judge one missed line. Writes the verdict ledger, its thought and a gap idea; says nothing itself.

    stage     where the fixed commands gave up (free text, shown to the model).
    fallback  what the house would have said anyway (e.g. "Sorry, I didn't catch that."); it is the
              answer when the line is not addressed to the assistant, the call fails, or it times out.
    """
    cfg, nb = s.cfg, s.nb
    t0 = now or time.time()
    body = ('RECENT:\n' + ('\n'.join(s.recent.lines(t0)) or '(nothing)') +
            f'\n\nNOW: {text!r}  (failed at: {stage})')

    def ask():                     # the context build and the model call both live inside the time budget
        return s.client.complete(system_prompt(s, query=text), body, cfg.screen_model, max_tokens=600)

    v = Verdict()
    t = time.time()
    try:
        d = json_from(s.run_with_timeout(ask, cfg.screen_timeout_s)) or {}
    except Exception as e:
        nb.log('screen call failed:', e.__class__.__name__, e)
        d, v.failed = {}, True
    v.elapsed = round(time.time() - t, 2)
    v.raw = d
    v.addressed = d.get('addressed') is True
    try:
        v.conf = float(d.get('conf') or 0)
    except (TypeError, ValueError):
        v.conf = 0.0
    v.intent = norm(str(d.get('intent') or ''))
    v.gap = norm(str(d.get('gap') or '')) if v.addressed else ''
    v.thought = norm(str(d.get('thought') or ''))
    v.reply = speakable(norm(str(d.get('reply') or ''))) if v.addressed else ''
    v.speak = (v.reply or fallback) if v.addressed else fallback
    nb.append_jsonl('verdicts.jsonl', {'ts': round(t0, 1), 'text': text, 'stage': stage, 'addressed': v.addressed,
                                       'conf': v.conf, 'intent': v.intent, 'gap': v.gap, 'reply': v.reply,
                                       'thought': v.thought, 'elapsed': v.elapsed, 'failed': v.failed,
                                       'fallback': bool(fallback)})
    nb.log(f'screen {v.elapsed:.1f}s addressed={v.addressed} conf={v.conf} gap={v.gap!r} failed={v.failed}')
    if v.thought:
        nb.think(v.thought)
    if v.gap:
        nb.idea(v.gap, 'offer')
    return v
