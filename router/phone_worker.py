"""The phone-miss worker: what answers when tier 0 had no phrase for it.

On the owner's phone, a spoken command that matches no phrase used to open the phone's stock
assistant (a search page). Instead it starts an Agent SDK session on Sonnet with the house's tools:

    phrase reflexes ("house"), HA reads, Kodi now-playing, the car, mail/calendar adapters,
    phone intents, food diary, a wishlist, plus WebSearch and WebFetch.

and speaks ONE short sentence back. Its permissions are NOT the spoken-permission rule of the
room's rung 2/3 sessions; read gate.py. In short: its own tools auto-approve (including writes that
stay in the house: a light, a destination sent to the car, a timer, a food-log line, a draft, an
event on the owner's own calendar), and everything that goes to someone else (send, reply,
forward, RSVP, share, delete) is blocked in every session. There is no yes/no channel on the
phone, so there is nothing to ask.

    registry = build_registry(home=..., reflex=..., car=..., mail=..., calendar=..., phone=...)
    worker = PhoneWorker(registry)                       # SdkRunner by default (needs claude_agent_sdk)
    worker.run("add dentist at 3 PM tomorrow")           # -> {'sentence', 'tools', 'secs', 'dry'}

Tests and the offline demo pass `runner=ScriptedRunner(steps)` from fakes.py instead.
"""
import asyncio
import os
import re
import time
import warnings

from . import gate
from .connectors import calendar, car, food, home, hosted, kodi, mail, phone, wish
from .registry import ToolRegistry

# allowed_tools + can_use_tool together make the SDK warn that the callback is skipped for
# pre-approved tools. That is the intent here (AUTO tier); see gate.py.
warnings.filterwarnings('ignore', message='can_use_tool will not be invoked')

MODEL = os.environ.get('ROUTER_WORKER_MODEL', 'claude-sonnet-4-5')
DEADLINE_S = 60          # the phone holds a pending answer for ~80 s counted after "On it."
BUILTIN_TOOLS = ('WebSearch', 'WebFetch')

PROMPT = """You are the house assistant, answering by voice on the owner's phone. It is {now}. {where}
Do what was asked using your tools, then reply with ONE short spoken sentence (two at most, under 30 words):
no markdown, no lists, no URLs, no em dashes, no offers of what else you could do.
- Facts you do not already have or that change (weather, opening hours, news, prices): WebSearch first.
- Home, TV, music, car climate: call `house` with ONE plain phrase the way the owner would say it. Only
  phrases the owner defined work; if it says it did not recognise one, say that, do not guess another.
- Only state facts a tool actually returned. If a tool failed or says DRY RUN, say so; never fill in numbers.
- You cannot send email, answer invites, delete anything or buy anything. If that is what was asked, say so
  in one sentence.
- If a request needs a tool you do not have, say "I can't do that yet" and note it with `wish`.
Your tools:
{tools}"""


def build_registry(home_backend=None, reflex=None, kodi_backend=None, car_backend=None, mail_backend=None,
                   calendar_backend=None, phone_backend=None, food_backend=None, wishlist_path=None):
    """Register a connector for each backend given; leave the rest out. A tool that is not
    registered does not exist for the model, which is the strongest 'blocked'."""
    reg = ToolRegistry('house')
    if reflex is not None or home_backend is not None:
        home.register(reg, home_backend, reflex)
    if kodi_backend is not None:
        kodi.register(reg, kodi_backend)
    if car_backend is not None:
        car.register(reg, car_backend)
    if mail_backend is not None:
        mail.register(reg, mail_backend)
    if calendar_backend is not None:
        calendar.register(reg, calendar_backend)
    if phone_backend is not None:
        phone.register(reg, phone_backend)
    if food_backend is not None:
        food.register(reg, food_backend)
    if wishlist_path:
        wish.register(reg, wishlist_path)
    return reg


def default_policy(registry, hosted_connectors=False, log=None):
    """Registry tiers + the built-in web tools; optionally the hosted connectors' classified names."""
    extra_auto = list(BUILTIN_TOOLS) + (hosted.AUTO if hosted_connectors else [])
    p = registry.policy(extra_auto=extra_auto, extra_blocked=hosted.BLOCKED if hosted_connectors else (), log=log)
    if hosted_connectors:
        p.rules.update(hosted.RULES)
    return p


def system_prompt(registry, away=False, now=None):
    where = ('The owner is AWAY from home: the house refuses "play" on the home TV.' if away
             else 'The owner is at home.')
    tools = '\n'.join(f'- {t.name}: {t.description}' for t in registry.tools.values())
    return PROMPT.format(now=now or time.strftime('%A %B %-d %Y, %-I:%M %p %Z'), where=where, tools=tools)


def speakable_reply(t):
    """A model reply as one thing worth saying aloud: no citations tail, links, markup or dashes."""
    t = re.split(r'\n?\s*Sources?:', t or '')[0]
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'[*_#`>]+', '', t).replace('\u2014', ', ').replace('\u2013', ' to ')
    t = re.sub(r'\s+', ' ', t).strip()
    return re.sub(r'\s+([,.;:!?])', r'\1', t)[:400]


class SdkRunner:
    """The real session: ClaudeSDKClient, in-process tool server, the policy's three layers."""

    def __init__(self, max_turns=12, extra_servers=None):
        self.max_turns, self.extra_servers = max_turns, extra_servers or {}

    def options(self, sdk, system_prompt, registry, policy, dry=False, model=None):
        # setting_sources=[]: the owner's own settings carry allow rules that would auto-approve
        # tools past the gate; this session gets exactly the tools and tiers built here.
        return sdk.ClaudeAgentOptions(
            model=model or MODEL, setting_sources=[], thinking={'type': 'disabled'}, effort='low',
            system_prompt=system_prompt, tools=list(BUILTIN_TOOLS), max_turns=self.max_turns,
            mcp_servers={registry.server: registry.sdk_server(sdk, dry), **self.extra_servers},
            **policy.sdk_kwargs(sdk))

    async def run(self, text, system_prompt, registry, policy, dry=False, model=None):
        import claude_agent_sdk as s
        said, used = [], []
        async with s.ClaudeSDKClient(options=self.options(s, system_prompt, registry, policy, dry, model)) as c:
            await c.query(text)
            async for m in c.receive_response():
                if isinstance(m, s.AssistantMessage):
                    for b in m.content:
                        if isinstance(b, s.ToolUseBlock):
                            used.append(b.name.rsplit('__', 1)[-1])
                        elif isinstance(b, s.TextBlock) and b.text.strip():
                            said.append(b.text.strip())
        return (said[-1] if said else ''), used


class PhoneWorker:
    def __init__(self, registry, policy=None, runner=None, model=None, deadline_s=DEADLINE_S):
        self.registry = registry
        self.policy = policy or default_policy(registry)
        self.runner = runner or SdkRunner()
        self.model, self.deadline_s = model, deadline_s

    def run(self, text, away=False, dry=False):
        """One request -> {'sentence', 'tools', 'secs', 'dry'}. Never raises: a spoken sentence always comes back."""
        t0 = time.time()
        prompt = system_prompt(self.registry, away)
        try:
            out, used = asyncio.run(asyncio.wait_for(
                self.runner.run(text, prompt, self.registry, self.policy, dry, self.model), self.deadline_s))
            sentence = speakable_reply(out) or 'I tried, but came up empty.'
        except asyncio.TimeoutError:
            sentence, used = 'That took too long, sorry. Ask me again on the desk.', []
        except Exception as e:                       # class name only: the text is the owner's own words
            sentence, used = "I couldn't reach Claude just now.", [f'error:{e.__class__.__name__}']
        return {'sentence': sentence, 'tools': used, 'secs': round(time.time() - t0, 1), 'dry': dry}
