"""One place that says what each tool is: its name, schema, whether it writes, and its permission tier.

The production worker had the same facts in three places (the tool wrapper, the ALLOW list, the
prompt). Here a tool is registered once and the rest is derived:

    reg = ToolRegistry('house')
    reg.add('car', 'Send a destination...', {'action': str, 'place': str}, car_fn, writes=True)
    reg.run('car', {'action': 'nav', 'place': 'museum'}, dry=True)   # -> 'DRY RUN: would car ...'
    policy = reg.policy(blocked=['mcp__mail__send'])                  # a gate.ToolPolicy
    server = reg.sdk_server(sdk)                                      # in-process MCP server

Two rules carried over from production, both learned from a live mistake:

  * `dry=True` never reaches a call that writes (`writes=True`, or `writes=fn(args)` for a tool
    like `car` where nav writes and battery does not). A "dry" test once sent a real navigation
    to a real car, so dry is enforced here, once, before any tool body runs.
  * A tool that fails returns the exception CLASS name, never its message: argv and request text
    can carry tokens or the user's own words, and the result lands in the model's context and logs.
"""
import asyncio
import inspect
from dataclasses import dataclass
from typing import Callable, Optional

from .gate import AUTO, BLOCKED, TIERS, ToolPolicy


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable
    writes: object = False             # bool, or fn(args) -> bool when only some calls write (car: nav yes, battery no)
    tier: str = AUTO
    rule: Optional[Callable] = None
    source: str = ''                   # which connector registered it, for the capability table


class ToolRegistry:
    def __init__(self, server='house'):
        self.server = server
        self.tools = {}

    def full_name(self, name):
        return f'mcp__{self.server}__{name}'

    def add(self, name, description, schema, fn, writes=False, tier=AUTO, rule=None, source=''):
        if name in self.tools:
            raise ValueError(f'tool {name!r} registered twice')
        if tier not in TIERS:
            raise ValueError(f'unknown tier {tier!r}')
        self.tools[name] = Tool(name, description, dict(schema), fn, writes, tier, rule, source)

    def run(self, name, args, dry=False):
        t = self.tools.get(name)
        if t is None:
            return f'Unknown tool {name}.'
        args = dict(args or {})
        if dry and (t.writes(args) if callable(t.writes) else t.writes):
            shown = ', '.join(f'{k}={v!r}' for k, v in args.items() if v not in ('', None, []))
            return f'DRY RUN: would call {name}({shown}).'
        try:
            inspect.signature(t.fn).bind(**args)
        except TypeError:                            # the model sent arguments the tool doesn't have
            return f'{name} was called with arguments it does not take.'
        try:
            return str(t.fn(**args))
        except Exception as e:                       # class name only, never the message
            return f'{name} failed ({e.__class__.__name__}).'

    def policy(self, extra_auto=(), extra_ask=(), extra_blocked=(), log=None):
        """A ToolPolicy from the registered tiers. `extra_*` are exact names of tools that live
        outside the registry (a hosted connector's, a built-in like WebSearch)."""
        names = lambda tier: [self.full_name(n) for n, t in self.tools.items() if t.tier == tier]
        rules = {self.full_name(n): t.rule for n, t in self.tools.items() if t.rule}
        return ToolPolicy(auto=names(AUTO) + list(extra_auto), ask=names('ask') + list(extra_ask),
                          blocked=names(BLOCKED) + list(extra_blocked), rules=rules, log=log)

    def sdk_server(self, sdk, dry=False):
        """An in-process MCP server exposing every registered tool (lazy: needs claude_agent_sdk)."""
        wrapped = []
        for t in self.tools.values():
            def make(t):
                @sdk.tool(t.name, t.description, t.schema)
                async def _call(args, t=t):
                    out = await asyncio.to_thread(self.run, t.name, args, dry)
                    return {'content': [{'type': 'text', 'text': out}]}
                return _call
            wrapped.append(make(t))
        return sdk.create_sdk_mcp_server(self.server, tools=wrapped)

    def rows(self):
        """One dict per tool, for the capability table and for tests."""
        return [{'tool': t.name, 'writes': 'some' if callable(t.writes) else bool(t.writes), 'tier': t.tier, 'source': t.source,
                 'description': t.description} for t in self.tools.values()]
