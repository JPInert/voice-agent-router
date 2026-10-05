"""The model tiers: everything tier 0 (tricks + hand-coded intents) could not claim.

    rung 1  Haiku, one tool-less call: classify the utterance and, if it is a
            question the system snapshot already answers, answer it right there.
    rung 2  a Haiku Agent SDK session with read tools + Bash, spoken permission
            for anything that writes, and a goal check after every turn.
    rung 3  the same on Sonnet with write/web tools: for jobs rung 1 marked hard,
            code changes, or a rung-2 attempt that fell short.

Never de-escalates. A spoken "no" (or no answer) ends the task: no goal-loop
retry, no escalation into a fresh session that would ask all over again.

Extracted from the production curator. Removed here: the HTTP server, the
session registry for interactive Claude Code tabs, and the live voice-over-a-
coding-tab mode. `Router.utterance()` is what the HTTP handler called.
"""
import concurrent.futures
import json
import os
import re
import threading
import time

from .permissions import bash_is_readonly, describe_tool, speak_cmd, speak_tool, SILENT_TOOLS
from .speaker import SentenceSpeaker, speakable

HAIKU = 'claude-haiku-4-5-20251001'
SONNET = 'claude-sonnet-5-5'
RUNG_MODEL = {1: HAIKU, 2: HAIKU, 3: SONNET}

RUNG1_DEADLINE = 1.05   # the voice daemon blocks on /utterance; past this, ack silently and finish async
GOAL_ROUNDS = 3         # done-check re-prompt cap (the SDK has no --goal)
TASK_ASK_MAX = 1        # questions a task may ask by voice. Every extra question asked into a room
                        # with the TV on captured the room, not an answer: one production run asked
                        # three times off one false wake. One question, then stop.


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
CLASSIFIER_SYS = """You classify ONE spoken utterance for a Star Trek style desktop computer \
(Debian/XFCE). The hand-coded reflexes already missed it, so it is not a known command.

Output ONE line of compact JSON and NOTHING else. No code fences, no prose.
Keys:
  kind      one of: answer, device, terminal, dev, web, unclear
  answer    a spoken reply, 1-2 short plain sentences, no markdown. EMPTY STRING
            unless kind=answer.
  question  ONE short question, phrased for speech (under 12 words, no dashes,
            no lists of technical options). EMPTY STRING unless kind=unclear.
  hard      true if the job needs several steps, real judgement, or writing code.
  done_when one short condition that a LATER COMMAND OR SNAPSHOT COULD CHECK,
            e.g. "bluetoothctl info shows Connected: yes", "lpstat -W completed
            lists the job", "the default sink volume is 40%". It must be about
            the MACHINE'S STATE, never about what was said to the user or what the
            worker did — speaking is verified separately. If nothing about the
            machine changes (a pure lookup), leave it EMPTY: any question that
            only READS state — "how many files…", "what is my…", "is X
            running" — gets EMPTY, never a condition about a command's output.
            EMPTY STRING when kind is answer or unclear.

Kinds:
  answer    general knowledge, or a fact you can read straight off the SNAPSHOT
            below (volume, temperature, free disk, which printer, paired
            headphones, failed services). Answer it in "answer". Never say you
            lack access to the machine — the snapshot IS the machine.
  device    controlling the house or the media box: lights, thermostat, TV,
            playback, screens, volume.
  terminal  something about THIS desktop a shell can inspect or change that the
            snapshot does not already answer: pair a device, print a file,
            restart a service, investigate a fault.
  dev       writing or changing code or configuration.
  web       needs the internet: look something up, browse, download.
  unclear   you genuinely cannot tell what is wanted, or a required detail is
            missing (which printer, which file). Ask ONE short question.

Prefer answer over terminal whenever the snapshot already contains the fact.
Prefer unclear over guessing when a detail you would have to invent is missing.
"""

TASK_SYS = """You are the hands of a Star Trek style desktop computer on a Debian / XFCE \
machine. The request arrived by voice and the answer goes back by voice.

How to work:
- Prefer an existing script in the workspace over improvised shell. Current library:
%(library)s
- When you improvise a command sequence that works, save it into the workspace as a
  small executable script with a one-line comment, so next time is one call.
- Read-only commands run silently. Anything that writes, installs, deletes or leaves the
  machine is read out to the user and needs a spoken yes, so batch such steps and keep
  them few and specific.
- If a required detail is missing (which printer, which file), call mcp__voice__ask ONCE with
  a short question instead of guessing. If the answer is absent or unclear, take it as no —
  never repeat or rephrase a question the user did not answer; report and stop instead.
- Report with mcp__voice__say: 1-3 plain spoken sentences, no markdown, no code, no lists.
  Say what you did and the result, not how you did it.
- Finish the job. Do not describe what could be done.

%(snapshot)s
"""

DONE_SYS = """You check whether a job is finished. You are given the original request, the \
condition that means done, what the worker reported, what it SPOKE ALOUD, and a FRESH
machine snapshot.
Output ONE line of compact JSON, no fences: {"met": true|false, "reason": "<short>"}
Rules:
- "met" is true when the evidence supports the condition. The spoken lines ARE the
  evidence that the user was told; you do not need any other proof of that.
- You cannot run commands and are never shown raw command output. The worker's report
  of what a command printed IS the record of that output — never answer false because
  the raw output "was not provided".
- Small numeric drift between the report and the snapshot (a temperature that moved a
  degree, free space that changed) is normal and is NOT a failure.
- Only answer false when the condition is genuinely unmet, and say concretely what is
  still missing so the worker can act on it."""


def json_from(text):
    """Pull the first JSON object out of a model reply (fences tolerated)."""
    if not text:
        return None
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r'\{.*\}', t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


class Classification(dict):
    @property
    def kind(self):
        return self.get('kind') or 'unclear'

    @property
    def hard(self):
        return bool(self.get('hard'))


# --- rung 1 without the model ----------------------------------------------
TRICK_VERBS = re.compile(
    r'\b(turn (on|off)|switch (on|off)|dim|brighten|lights?\b|play|pause|resume|stop|'
    r'skip|rewind|volume|mute|unmute|louder|quieter|good ?night|good ?morning|'
    r'movie night|screens? off|thermostat|set the (heat|ac|air))\b', re.I)


def heuristic(text):
    """A short utterance carrying a device verb is a device job by inspection — no point
    spending ~3 s of Haiku deciding that "turn on the kitchen light" is about a device.
    The trade is no `done_when` for those; device jobs are checked by the device."""
    low = (text or '').strip().lower()
    if low and TRICK_VERBS.search(low) and len(low.split()) <= 8:
        return Classification({'kind': 'device', 'answer': '', 'question': '',
                               'hard': False, 'done_when': ''})
    return None


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------
CANT_RE = re.compile(r"\b(i can'?t|i cannot|unable to|i wasn'?t able|no way to|"
                     r"not possible|i don'?t have (the )?(access|permission))\b", re.I)

VOICE_TOOLS = ['mcp__voice__say', 'mcp__voice__ask', 'mcp__voice__snapshot']
RUNG2_TOOLS = VOICE_TOOLS + ['Bash', 'Read', 'Glob', 'Grep']
RUNG3_TOOLS = RUNG2_TOOLS + ['Write', 'Edit', 'WebFetch', 'WebSearch', 'TodoWrite']
# NOTE: this roster goes in `tools=`, NEVER in `allowed_tools=`. An allowed_tools entry
# that allows a whole tool AUTO-APPROVES it before can_use_tool is consulted, which would
# silently void the spoken-permission rule. Measured: with allowed_tools=['Bash'] a
# `touch` ran with no callback and no question; with tools=['Bash'] the same call
# reached can_use_tool and was denied.


class Router:
    """voice: anything with say(text) and ask(text, mode, timeout_s) — see voice_io.py.
    snapshot: a callable returning a compact text description of the machine."""

    def __init__(self, voice, snapshot=None, workspace=None):
        self.voice = voice
        self._snapshot_fn = snapshot or (lambda: '')
        self._snap = {'text': '', 'ts': 0.0}
        self.workspace = workspace or os.path.join(os.path.expanduser('~'), '.local', 'share',
                                                   'voice-agent-router', 'workspace')
        self._seq = 0
        self._sdk = None
        self._loop = None
        self._loop_ready = threading.Event()
        threading.Thread(target=self._loop_thread, daemon=True).start()

    # --- plumbing: every SDK call runs on ONE asyncio loop in a background thread;
    # callers get concurrent futures, which is what the rung-1 deadline race needs.
    # This is also why a CPU spin inside a coroutine is so dangerous: the HTTP side
    # keeps answering while nothing on the loop is ever scheduled again.
    def _loop_thread(self):
        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def submit(self, coro):
        import asyncio
        self._loop_ready.wait(5)
        f = asyncio.run_coroutine_threadsafe(coro, self._loop)
        f.add_done_callback(_log_exc)
        return f

    @property
    def sdk(self):
        if self._sdk is None:
            import claude_agent_sdk
            self._sdk = claude_agent_sdk
        return self._sdk

    def snapshot(self, max_age=300, force=False):
        now = time.time()
        if force or now - self._snap['ts'] > max_age or not self._snap['text']:
            try:
                self._snap = {'text': self._snapshot_fn() or '', 'ts': now}
            except Exception as e:
                log('snapshot failed:', e)
        return self._snap['text']

    def _options(self, model, **kw):
        # setting_sources=[]: spawned sessions must NOT load the user's hooks and MCP
        # servers, or they register themselves with this router and post their own
        # events back to it — a feedback loop. The in-process voice server replaces them.
        opts = dict(model=model, setting_sources=[], thinking={'type': 'disabled'})
        opts.update(kw)
        return self.sdk.ClaudeAgentOptions(**opts)

    async def _one_shot(self, system_prompt, user_text, model=HAIKU, max_turns=2):
        """A single tool-less model call. max_turns=2, not 1: on an imperative utterance
        Haiku sometimes tries to CALL a tool instead of classifying; with tools=[] that
        still burns a turn, and at max_turns=1 the whole query died. Partial text is kept."""
        s = self.sdk
        opts = self._options(model, system_prompt=system_prompt, tools=[],
                             max_turns=max_turns, effort='low')
        out = []
        try:
            async for m in s.query(prompt=user_text, options=opts):
                if isinstance(m, s.AssistantMessage):
                    out.extend(b.text for b in m.content if isinstance(b, s.TextBlock))
        except Exception as e:
            if not out:
                raise
            log(f'one-shot ended early ({e.__class__.__name__}) — keeping partial text')
        return ''.join(out)

    # --- rung 1
    async def classify(self, text):
        raw = await self._one_shot(CLASSIFIER_SYS + '\n\n' + (self.snapshot() or '(no snapshot)'),
                                   text, model=RUNG_MODEL[1])
        d = json_from(raw) or {}
        if not d.get('kind'):
            log('rung1 unparseable reply:', (raw or '')[:200].replace('\n', ' '))
            d = {'kind': 'unclear', 'question': 'Say that again?', 'hard': False}
        for k in ('answer', 'question', 'done_when'):
            d.setdefault(k, '')
        d['hard'] = bool(d.get('hard'))
        return Classification(d)

    async def done_check(self, request, condition, report, spoken=()):
        said = '\n'.join(f'  - {x}' for x in spoken) or '  (nothing spoken)'
        body = (f'Request: {request}\nDone when: {condition}\n'
                f'Worker reported: {report[:1500]}\nSpoken aloud to the user:\n{said}\n\n'
                f'{self.snapshot(force=True)}')
        d = json_from(await self._one_shot(DONE_SYS, body, model=RUNG_MODEL[1])) or {}
        return bool(d.get('met')), (d.get('reason') or '')[:200]

    # --- tools the worker sessions get
    def _voice_server(self, spoken):
        s, voice = self.sdk, self.voice
        asked = [0]

        @s.tool('say', 'Speak a short line to the user out loud. 1-3 plain sentences, no markdown.',
                {'text': str})
        async def _say(args):
            import asyncio
            text = args.get('text', '')
            if text:
                spoken.append(text)
            await asyncio.to_thread(voice.say, text)
            return {'content': [{'type': 'text', 'text': 'spoken'}]}

        @s.tool('ask', 'Ask the user ONE short question out loud and wait for the spoken answer. '
                       'mode "yesno" resolves yes/no, mode "text" returns what was said.',
                {'text': str, 'mode': str})
        async def _ask(args):
            import asyncio
            if asked[0] >= TASK_ASK_MAX:
                # a normal result, not an error: an error invites the SDK to retry
                return {'content': [{'type': 'text', 'text':
                        'ASK LIMIT REACHED. Do not ask again. If you cannot proceed on what '
                        'you already have, say so briefly and stop.'}]}
            asked[0] += 1
            r = await asyncio.to_thread(voice.ask, args.get('text', ''),
                                        args.get('mode') or 'text', 12)
            return {'content': [{'type': 'text', 'text': json.dumps(r or {})}]}

        @s.tool('snapshot', 'A fresh compact snapshot of this machine: audio, bluetooth, '
                            'printers, displays, temps, disks, failed units.', {})
        async def _snapshot(args):
            import asyncio
            r = await asyncio.to_thread(self.snapshot, 0, True)
            return {'content': [{'type': 'text', 'text': r or '(unavailable)'}]}

        return s.create_sdk_mcp_server(name='voice', version='1.0.0',
                                       tools=[_say, _ask, _snapshot])

    def _can_use_tool(self, entry):
        """Permission is spoken. Read-only is silent; everything else is ONE question per task."""
        s, voice = self.sdk, self.voice

        async def can_use_tool(tool_name, input_data, context):
            import asyncio
            if tool_name in SILENT_TOOLS:
                return s.PermissionResultAllow()
            if tool_name == 'Bash' and bash_is_readonly(input_data.get('command', '')):
                log(f"[{entry['name']}] silent bash: {input_data.get('command', '')[:120]}")
                return s.PermissionResultAllow()
            if entry.get('denied'):
                # after one refusal every later write auto-denies SILENTLY — prompt wording
                # alone did not stop Haiku re-asking the same write two seconds later
                log(f"[{entry['name']}] auto-deny (already denied once): {tool_name}")
                return s.PermissionResultDeny(
                    message='The user already declined a step of this task. Do not attempt '
                            'any more writes; report what you could not do and stop.')
            what = describe_tool(tool_name, input_data)
            if tool_name == 'Bash':
                spoken = f'Run {speak_cmd(what)}?'
            else:
                arg = speak_cmd(re.sub(r'^\w+=', '', what)) if what else ''
                spoken = f'{speak_tool(tool_name)} {arg}. OK?' if arg else f'{speak_tool(tool_name)}. OK?'
            log(f"[{entry['name']}] ASK permission ({tool_name}): {spoken[:160]}")
            r = await asyncio.to_thread(voice.ask, spoken, 'yesno', 12) or {}
            # a write needs a CLEAR yes: a TV "Yes." in the background once came back
            # yes=True, unclear=True and would have approved a mkdir chain
            if r.get('yes') is True and not r.get('unclear'):
                return s.PermissionResultAllow()
            log(f"[{entry['name']}] permission denied (heard={r.get('heard')!r})")
            entry['denied'] = spoken
            # "find another way" (the first wording) made the worker re-ask the same write
            # as a bigger compound command — a denial must end the attempt, not spawn variants
            return s.PermissionResultDeny(
                message=('The user said no. Do not retry this or any variant of it; '
                         'report what you could not do and stop.' if r.get('yes') is False else
                         'No clear answer — treat it as no. Do not ask again for this or a '
                         'variant; say briefly what needed permission and stop.'))

        return can_use_tool

    # --- rungs 2/3
    def _library(self):
        try:
            names = sorted(n for n in os.listdir(self.workspace)
                           if not n.startswith('.') and os.path.isfile(os.path.join(self.workspace, n)))
        except OSError:
            names = []
        return '\n'.join('  ' + n for n in names) or '  (empty — you are the first; save what works)'

    async def run_task(self, task_text, rung, kind, done_when=''):
        """Start a headless SDK session, then verify the goal: after each turn a cheap Haiku
        done-check reads the request, the worker's report and a FRESH snapshot; not met ->
        re-prompt the same session with the reason, up to GOAL_ROUNDS."""
        s = self.sdk
        model = RUNG_MODEL[rung]
        os.makedirs(self.workspace, exist_ok=True)
        self._seq += 1
        entry = {'name': f'{kind}-{self._seq}'}
        spoken = []
        opts = self._options(
            model,
            system_prompt=TASK_SYS % {'library': self._library(), 'snapshot': self.snapshot()},
            cwd=self.workspace,
            tools=RUNG3_TOOLS if rung >= 3 else RUNG2_TOOLS,
            mcp_servers={'voice': self._voice_server(spoken)},
            can_use_tool=self._can_use_tool(entry),
            permission_mode='default',
            max_turns=40 if rung >= 3 else 16,
        )
        t0, tool_errors, rounds, met, reason, report = time.time(), 0, 0, False, '', ''
        try:
            async with s.ClaudeSDKClient(options=opts) as client:
                prompt = task_text
                while rounds < GOAL_ROUNDS:
                    rounds += 1
                    # streamed prose is the model thinking out loud; the say tool is the
                    # channel. Prose is collected, and spoken only if the session never spoke.
                    speaker = SentenceSpeaker(say=self.voice.say, enabled=False)
                    await client.query(prompt)
                    async for m in client.receive_response():
                        if isinstance(m, s.AssistantMessage):
                            for b in m.content:
                                if isinstance(b, s.TextBlock):
                                    speaker.feed(b.text)
                        elif isinstance(m, s.UserMessage):
                            for b in (m.content if isinstance(m.content, list) else []):
                                if isinstance(b, s.ToolResultBlock) and getattr(b, 'is_error', False):
                                    tool_errors += 1
                        elif isinstance(m, s.ResultMessage):
                            report = (m.result or '')[:4000]
                    speaker.flush()
                    report = report or ' '.join(speaker.spoken)
                    if not done_when:
                        met = True
                        break
                    met, reason = await self.done_check(task_text, done_when, report, spoken)
                    log(f"[{entry['name']}] goal={done_when!r} met={met} rounds={rounds} reason={reason!r}")
                    if met:
                        break
                    if entry.get('denied'):
                        reason = 'a step needed your OK'
                        break
                    prompt = f'Not done yet: {reason}. Keep going until this is true: {done_when}'
            if met and not spoken and report:
                self.voice.say(speakable(report))
        except Exception as e:
            log(f"[{entry['name']}] task crashed:", repr(e))
            reason = str(e)[:200]
        elapsed = time.time() - t0
        log(f"[{entry['name']}] rung={rung} model={model} elapsed={elapsed:.2f}s "
            f"rounds={rounds} met={met} tool_errors={tool_errors}")
        return {'name': entry['name'], 'met': met, 'reason': reason, 'report': report,
                'spoken': spoken, 'tool_errors': tool_errors, 'rounds': rounds,
                'rung': rung, 'elapsed': elapsed, 'denied': entry.get('denied')}

    async def dispatch(self, cls, text):
        rung = 3 if (cls.hard or cls.kind == 'dev') else 2
        res = await self.run_task(text, rung, cls.kind, cls.get('done_when') or '')
        # never escalate past a spoken denial — a fresh session would only ask again
        if rung == 2 and not res.get('denied') and (
                not res['met'] or res['tool_errors'] >= 2 or CANT_RE.search(res['report'] or '')):
            log(f"escalating {res['name']} to rung 3")
            self.voice.say('Let me try that properly.')
            res = await self.run_task(
                f"{text}\n\n(A first attempt with a smaller model did not finish. What it "
                f"reported: {res['report'][:800]} — reason it fell short: {res['reason']})",
                3, cls.kind, cls.get('done_when') or '')
        if not res['met']:
            self.voice.say("I stopped there — that needed your OK." if res.get('denied') else
                           f"I couldn't finish that. {res['reason'][:160]}".strip())
        return res

    def _finish(self, cls, text):
        if cls.kind == 'answer':
            self.voice.say(cls.get('answer') or "I'm not sure about that one.")
        elif cls.kind == 'unclear':
            # one short question at most, and only from rung 1's own text; a second
            # clarifying round never converged in production (all of them came back unclear)
            log(f'unclear, dropped: {text!r}')
            self.voice.say("I didn't get that.")
        else:
            self.voice.say('on it')       # ack only once there is known work to do
            self.submit(self.dispatch(cls, text))

    def utterance(self, text):
        """Entry point for everything tier 0 missed. Returns within ~RUNG1_DEADLINE:
        the voice daemon's main loop is blocked on this call.

        Returns the spoken answer if rung 1 beat the deadline with one, '' if the work
        continues asynchronously (the daemon plays an earcon), None on error."""
        t0 = time.time()
        h = heuristic(text)
        if h is not None:
            log(f'utterance {text!r} rung=1 model=heuristic kind={h.kind}')
            threading.Thread(target=self._finish, args=(h, text), daemon=True).start()
            return ''
        fut = self.submit(self.classify(text))
        try:
            cls = fut.result(timeout=max(0.05, RUNG1_DEADLINE - (time.time() - t0)))
        except (concurrent.futures.TimeoutError, TimeoutError):
            log(f'utterance {text!r} rung=1 ack=silent (classifier still running)')
            threading.Thread(target=self._await_and_finish, args=(fut, text, t0), daemon=True).start()
            return ''
        except Exception as e:
            log('rung1 error:', repr(e))
            return None
        log(f'utterance {text!r} rung=1 model={RUNG_MODEL[1]} elapsed={time.time() - t0:.2f}s -> {dict(cls)}')
        if cls.kind == 'answer' and cls.get('answer'):
            return cls['answer']
        threading.Thread(target=self._finish, args=(cls, text), daemon=True).start()
        return ''

    def _await_and_finish(self, fut, text, t0):
        try:
            cls = fut.result(timeout=180)
        except Exception as e:
            fut.cancel()
            log('rung1 failed:', repr(e))
            self.voice.say("Sorry, I couldn't work that one out.")
            return
        log(f'utterance {text!r} rung=1 model={RUNG_MODEL[1]} elapsed={time.time() - t0:.2f}s -> {dict(cls)}')
        self._finish(cls, text)


def _log_exc(fut):
    try:
        fut.result()
    except concurrent.futures.CancelledError:
        pass
    except Exception as e:
        log('background task failed:', repr(e))
