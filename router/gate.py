"""Permission gate for tool-using sessions that have no spoken yes/no channel.

The rung 2/3 sessions in ladder.py ask out loud before a write (permissions.py). The phone-miss
worker (phone_worker.py) cannot: the phone has no way to answer a question mid-task. So its safety
is a different shape, and this module is that shape. Every tool the worker can see is in exactly
one tier:

    AUTO     runs with no question. Reads, and writes that stay inside the house.
    ASK      runs only after a spoken yes, when the caller supplies an `ask` function.
             Without one it is refused: a missing yes/no channel never counts as a yes.
    BLOCKED  never runs, in any session. Anything that leaves the house or cannot be undone.

Anything not named in a tier is refused (deny by default). A tool may also carry a `rule`: a small
function that inspects the arguments and can refuse, or rewrite them (an event may be added to the
owner's own calendar, never with attendees, and never notifying anyone).

How the tiers reach the Agent SDK matters, because the SDK has three layers:

    allowed_tools     auto-approves BEFORE the can_use_tool callback is consulted
    disallowed_tools  the only deny the callback cannot bypass
    can_use_tool      everything else lands here

so `sdk_kwargs()` puts AUTO into allowed_tools (that is the point of the tier), BLOCKED into
disallowed_tools, and routes the rest, rules and unknowns, through one callback. It is the
opposite of the spoken-permission rule in ladder.py, where putting a tool in allowed_tools would
silently skip the question. Different tiers, same SDK; see docs/CAPABILITIES.md.

`PlanSlot` is the other gate: a bigger job is proposed first, nothing runs on "go ahead" alone, and
a second explicit "confirm" within a minute is what starts it.
"""
import re
import time
from collections import deque
from typing import Callable, NamedTuple, Optional

AUTO, ASK, BLOCKED = 'auto', 'ask', 'blocked'
TIERS = (AUTO, ASK, BLOCKED)


class Verdict(NamedTuple):
    tier: str                              # AUTO / ASK / BLOCKED: what happens to this call
    message: str = ''                      # shown to the model when refused
    updated_input: Optional[dict] = None   # a rule may rewrite the arguments


class ToolPolicy:
    """auto / ask / blocked are collections of exact tool names. `rules` maps a tool name to
    fn(input) -> Verdict | None (None = no opinion, fall back to the tool's tier)."""

    def __init__(self, auto=(), ask=(), blocked=(), rules=None, log=None, history=50):
        self.auto, self.ask, self.blocked = set(auto), set(ask), set(blocked)
        both = (self.auto & self.ask) | (self.auto & self.blocked) | (self.ask & self.blocked)
        if both:
            raise ValueError(f'tool in more than one tier: {sorted(both)}')
        self.rules = dict(rules or {})
        self.log = log or (lambda *a: None)
        self.decisions = deque(maxlen=history)         # (tool, tier) newest last, for logs and tests

    def tier(self, name):
        if name in self.blocked:
            return BLOCKED
        if name in self.ask:
            return ASK
        if name in self.auto:
            return AUTO
        return BLOCKED                                  # unknown: deny by default

    def decide(self, name, inp=None, ask=None):
        """The verdict for one call. `ask(prompt) -> bool` is the spoken yes/no, when there is one."""
        inp = inp or {}
        tier = self.tier(name)
        if tier == BLOCKED:
            v = Verdict(BLOCKED, 'Not allowed from the voice assistant.')
        else:
            rule = self.rules.get(name)
            ruled = rule(inp) if rule else None
            if ruled is not None and ruled.tier == BLOCKED:
                v = ruled
            elif tier == ASK:
                if ask is not None and ask(f'OK to run {name.rsplit("__", 1)[-1]}?'):
                    v = Verdict(AUTO, '', ruled.updated_input if ruled else None)
                else:
                    v = Verdict(BLOCKED, 'Needs a spoken yes and none was given. Say so and stop.')
            else:
                v = Verdict(AUTO, '', ruled.updated_input if ruled else None)
        self.decisions.append((name, v.tier))
        self.log(f'gate {name}: {v.tier}')
        return v

    def sdk_kwargs(self, sdk, ask=None):
        """ClaudeAgentOptions keyword arguments. `sdk` is the claude_agent_sdk module (or a stand-in)."""
        async def can_use_tool(name, inp, ctx):
            v = self.decide(name, inp, ask)
            if v.tier == AUTO:
                return sdk.PermissionResultAllow(updated_input=v.updated_input) if v.updated_input \
                    else sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message=v.message)

        # a rule-bound AUTO tool must reach the callback, so it is NOT auto-approved up front
        pre_approved = sorted(n for n in self.auto if n not in self.rules)
        return {'allowed_tools': pre_approved, 'disallowed_tools': sorted(self.blocked),
                'can_use_tool': can_use_tool}


def no_attendees(inp):
    """Calendar rule: only the owner's own calendar, never an invite. Applies to any event-add tool."""
    if inp.get('attendees') or inp.get('attendeeEmails'):
        return Verdict(BLOCKED, 'No attendees: the owner\'s own calendar only.')
    return Verdict(AUTO, '', dict(inp, notificationLevel='NONE'))


# ---------------------------------------------------------------------------
# the second yes
# ---------------------------------------------------------------------------
APPROVE_RE = re.compile(r"^\W*(?:(?:yes|yeah|ok(?:ay)?|computer)\W+)*(?:go ahead|do it|do that|yes do (?:it|that)|"
                        r"make it so|approved?|proceed|yes please|go for it)(?:\W+(?:please|then|now))*\W*$", re.I)
CONFIRM_RE = re.compile(r"^\W*(?:(?:yes|computer)\W+)*confirm(?:ed)?(?:\W+(?:it|that))?\W*$", re.I)


class PlanSlot:
    """ONE pending plan. propose() -> approve() arms it and reads it back -> confirm() within
    `confirm_s` releases it, once. A plan expires after `plan_ttl_s`. Time is injected for tests."""

    def __init__(self, plan_ttl_s=600, confirm_s=60, clock=time.time):
        self.plan_ttl_s, self.confirm_s, self.clock = plan_ttl_s, confirm_s, clock
        self.plan = None

    def propose(self, text):
        self.plan = {'text': text, 'expires': self.clock() + self.plan_ttl_s, 'armed': None, 'used': False}

    def _live(self):
        p = self.plan
        return p if p and not p['used'] and self.clock() < p['expires'] else None

    def state(self):
        p = self._live()
        return {'open': bool(p), 'armed': bool(p and p['armed'] and self.clock() - p['armed'] <= self.confirm_s)}

    def approve(self):
        p = self._live()
        if not p:
            return 'There is no plan waiting for approval.'
        p['armed'] = self.clock()
        return f"I'll {p['text']} Say computer, confirm within a minute."

    def confirm(self):
        """-> (plan text, None) when released, (None, reason) when not."""
        p = self._live()
        if not p:
            return None, 'That plan has expired or was already done; ask for it again.'
        if not p['armed'] or self.clock() - p['armed'] > self.confirm_s:
            return None, 'Nothing is waiting for a confirm. Say go ahead first, then confirm.'
        p['used'] = True
        return p['text'], None

    def intercept(self, text):
        """'approve' / 'confirm' / None. Matched by regex before any model, and only when there is
        something for the words to act on: without an open plan "yes please" is just conversation."""
        st = self.state()
        if CONFIRM_RE.match(text) and st['armed']:
            return 'confirm'
        if APPROVE_RE.match(text) and st['open']:
            return 'approve'
        return None
