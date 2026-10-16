"""In-memory stand-ins for every backend protocol, so the connectors, the gate and the phone worker
run offline: no Home Assistant, no car, no phone, no mailbox, no model.

Each fake records what it was asked to do (`.calls`), which is what the tests assert on: for a
blocked or dry call the list stays empty. `ScriptedRunner` stands in for the model, replaying a
fixed list of steps through the SAME ToolPolicy and ToolRegistry the real session uses.
"""
import json
import subprocess

from .gate import AUTO


class FakeHome:
    """Implements both halves the router uses: call()/process() for trick actions, states() for reads."""

    def __init__(self, states=None):
        self._states, self.calls, self.processed = states or [], [], []

    def call(self, domain, service, data):
        self.calls.append((domain, service, data))

    def process(self, text):
        self.processed.append(text)
        return True, 'Done.'

    def states(self):
        return self._states


class FakeKodi:
    def __init__(self, players=None, item=None, props=None):
        self.players, self.item, self.props, self.calls = players or [], item or {}, props or {}, []

    def rpc(self, method, params=None):
        self.calls.append(method)
        if method == 'Player.GetActivePlayers':
            return self.players
        if method == 'Player.GetItem':
            return {'item': self.item}
        return self.props


class FakeCar:
    """state 'online' answers at once; 'asleep' makes nav time out like a car that is still waking."""

    def __init__(self, state='online', history=None):
        self._state, self._history, self.calls = state, history or [], []

    def act(self, action, place=''):
        self.calls.append((action, place))
        if action in ('nav', 'stop') and self._state == 'asleep':
            raise subprocess.TimeoutExpired('car', 20)
        if action == 'nav':
            return {'ok': True, 'sentence': f'Sent {place} to the car.'}
        if action == 'stop':
            return {'ok': True, 'pending': True}
        return {'ok': True, 'sentence': {'battery': 'The car is at 64 percent.',
                                         'temp': 'It is 71 degrees inside.',
                                         'status': 'The car is online and parked.'}[action]}

    def state(self):
        return self._state

    def history(self):
        return self._history


class FakeMail:
    """There is deliberately no send() here either."""

    def __init__(self, messages=None):
        self.messages, self.drafts = messages or [], []

    def search(self, query):
        q = query.lower()
        return [m for m in self.messages if q in (m['subject'] + m['from']).lower()]

    def get(self, id):
        return next((m for m in self.messages if m['id'] == id), None)

    def create_draft(self, to, subject, body):
        self.drafts.append({'to': to, 'subject': subject, 'body': body})


class FakeCalendar:
    def __init__(self, events=None):
        self.events, self.added = events or [], []

    def list_events(self, start, end):
        return self.events

    def add_event(self, title, start, end, description):
        self.added.append({'title': title, 'start': start, 'end': end, 'description': description})


class FakePhone:
    def __init__(self, battery='level: 82'):
        self._battery, self.calls = battery, []

    def open_url(self, url):
        self.calls.append(('open_url', url))
        return True

    def timer(self, seconds, label=''):
        self.calls.append(('timer', seconds, label))
        return True

    def alarm(self, hhmm, label=''):
        self.calls.append(('alarm', hhmm, label))
        return True

    def battery(self):
        return self._battery

    def open_app(self, word):
        self.calls.append(('open_app', word))
        return word

    def run(self, argv):
        return ''


class FakeFood:
    def __init__(self):
        self.calls = []

    def run(self, argv):
        self.calls.append(argv)
        return json.dumps({'ok': True, 'argv': argv})


class ScriptedRunner:
    """Replays steps instead of asking a model. A step is ('tool', full_tool_name, args) or
    ('say', text); the last 'say' is the reply. Every tool step passes through the real ToolPolicy
    first, so a blocked call is refused here exactly as the SDK callback would refuse it."""

    def __init__(self, steps):
        self.steps, self.transcript = list(steps), []

    async def run(self, text, system_prompt, registry, policy, dry=False, model=None):
        said, used = [], []
        for step in self.steps:
            if step[0] == 'say':
                said.append(step[1])
                continue
            _, full, args = step
            name = full.rsplit('__', 1)[-1]
            used.append(name)
            v = policy.decide(full, args)
            if v.tier == AUTO:
                out = registry.run(name, v.updated_input or args, dry) if full.startswith(registry.full_name('')) \
                    else f'(hosted tool {name}: not available offline)'
            else:
                out = f'DENIED: {v.message}'
            self.transcript.append((full, args, v.tier, out))
        return (said[-1] if said else ''), used
