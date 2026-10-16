"""The car: destinations and reads, nothing that locks, unlocks or opens it.

The tool takes six actions and only six:

    nav <place>    send a destination            (writes; wakes the car without asking)
    stop <place>   add a stop before it          (writes)
    battery / temp / status                      (reads; never wake the car)
    history        where recent destinations went (reads a local file)

Lock, unlock, trunk, windows, honk, flash and sentry are not actions here and never will be: the
tool is the allow-list. Climate and media are phrase reflexes in tricks.yaml instead, because the
rule "ask before waking a sleeping car" lives with them.

`CliCarBackend` drives a command-line client that prints one JSON object per call
(tesla-fleet-voice's `car.py` is one: `car.py nav "coffee shop" --say`). A nav that is still
waking the car at 20 s is reported as "still sending", not as a failure: the service finishes it.
"""
import json
import subprocess

ACTIONS = ('nav', 'stop', 'battery', 'temp', 'status', 'history')
WRITES = ('nav', 'stop')


class CliCarBackend:
    def __init__(self, argv, timeout=20, history_path=None):
        self.argv, self.timeout, self.history_path = list(argv), timeout, history_path

    def _run(self, *args, timeout=None):
        r = subprocess.run(self.argv + list(args), capture_output=True, text=True, timeout=timeout or self.timeout)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1]), r.returncode
        except (ValueError, IndexError):
            return {}, r.returncode

    def act(self, action, place=''):
        """-> {'sentence': ..., 'state': ..., 'pending': ...}. Raises subprocess.TimeoutExpired."""
        res, rc = self._run(action, *([place] if place else []))
        res.setdefault('ok', rc == 0)
        return res

    def state(self):
        return self._run('status', timeout=5)[0].get('state')

    def history(self):
        if not self.history_path:
            return []
        try:
            return json.load(open(self.history_path))[:15]
        except (OSError, ValueError):
            return []


def car(backend, action, place=''):
    action = str(action or '').strip().lower()
    place = ' '.join(str(place or '').split())[:120]
    if action not in ACTIONS:
        return f'Car actions: {", ".join(ACTIONS)}.'
    if action == 'history':
        hist = backend.history()
        return ('Destinations SENT to the car, newest first (not proof it drove there):\n' +
                '\n'.join(f"{h.get('label') or h.get('q')} (asked as \"{h.get('q')}\")" for h in hist)) \
            if hist else 'No destination history yet.'
    if action in WRITES and not place:
        return 'Which place?'
    try:
        res = backend.act(action, place)
    except subprocess.TimeoutExpired:
        return ('Still sending it to the car; it goes in shortly.' if backend.state() == 'online'
                else 'The car is still waking up; the destination goes in when it is awake.')
    except Exception as e:                                    # class name only
        return f"Couldn't reach the car service ({e.__class__.__name__})."
    if res.get('pending'):
        return f'Adding {place} as a stop now; the car will say when it is in.'
    return res.get('sentence') or (f"The car is {res['state']}." if res.get('state')
                                   else 'Done.' if res.get('ok', True) else 'The car did not take it.')


def register(reg, backend):
    reg.add('car', 'The car: action=nav (place = where, as the owner described it), stop (add a stop before the '
                   'destination), battery, temp, status (reads), history (recent destinations).',
            {'action': str, 'place': str},
            lambda action='', place='': car(backend, action, place),
            writes=lambda a: str(a.get('action', '')).strip().lower() in WRITES, source='car')
