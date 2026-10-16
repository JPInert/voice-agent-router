"""Home Assistant: the reflex path (a phrase -> trick actions) and a read-only state tool.

Two tools, deliberately different:

    house(command)       a plain phrase, run through the SAME tier-0 tricks the voice daemon uses
                         ("lights off", "movie night"). Writes, but only what a trick file names, so
                         the model can never invent an entity id or a service call.
    house_status(query)  reads states: locks, doors, temperatures. Never writes.

Because `house` goes through tricks, a trick marked `confirm: true` needs a spoken yes the phone
cannot give; here that is refused out loud ("I can't confirm that from the phone.") rather than assumed.
"""
import json
import re
import urllib.request

from .. import tricks


class HttpHome:
    """Home Assistant's REST API. url like http://homeassistant.local:8123, token = a long-lived token."""

    def __init__(self, url, token, timeout=8):
        self.url, self.timeout = url.rstrip('/'), timeout
        self.hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    def _open(self, path, data=None):
        req = urllib.request.Request(self.url + path, headers=self.hdrs,
                                     data=None if data is None else json.dumps(data).encode(),
                                     method='GET' if data is None else 'POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b'null')

    def call(self, domain, service, data):
        self._open(f'/api/services/{domain}/{service}', data)

    def states(self):
        return self._open('/api/states')

    def process(self, text):
        """Assist's own grammar. -> (matched, speech); matched is None when it understood a device
        command but found no target. NOTE this EXECUTES whatever it matches: never use it as a test."""
        resp = self._open('/api/conversation/process', {'text': text, 'language': 'en'})['response']
        speech = resp.get('speech', {}).get('plain', {}).get('speech', '')
        if resp.get('response_type') == 'error':
            return (None, 'No such device.') if resp.get('data', {}).get('code') == 'no_valid_targets' \
                else (False, speech)
        return True, 'Done.' if speech in ('Started', 'Done') else speech


def house_status(home, query=''):
    """States as short lines. `query` = words to match in the entity id or name; empty = locks,
    doors, thermostats, temperatures. Most-matching entities first, at most 60 lines."""
    states = home.states()
    words = [w for w in re.findall(r'[a-z0-9]+', str(query).lower()) if len(w) > 2]
    default = ('lock.', 'climate.', 'binary_sensor.', 'sensor.')
    out = []
    for st in states:
        eid, attrs = st.get('entity_id', ''), st.get('attributes') or {}
        name = str(attrs.get('friendly_name') or eid)
        hay = f'{eid} {name}'.lower()
        if words and not any(w in hay for w in words):
            continue
        if not words and not (eid.startswith(default) and re.search(r'lock|door|temp|humid|thermo', hay)):
            continue
        unit = attrs.get('unit_of_measurement') or ''
        extra = f" (set {attrs['temperature']})" if eid.startswith('climate.') and attrs.get('temperature') else ''
        score = sum(w in hay for w in words)
        out.append((-score, f"{name}: {st.get('state')}{unit and ' ' + unit}{extra}"))
    return '\n'.join(t for _, t in sorted(out)[:60]) or f'No Home Assistant entity matches "{query}".'


class Reflex:
    """The tier-0 trick list as a callable: reflex(phrase) -> (matched, said_lines).

    Built from a tricks.Loader (hot reload) and the backends the trick actions may touch.
    Nothing here asks a model anything."""

    def __init__(self, loader, ha=None, kodi_play=None, scripts_dir=None, log=None):
        self.loader, self.ha, self.kodi_play, self.scripts_dir = loader, ha, kodi_play, scripts_dir
        self.log = log or (lambda *a: None)

    def __call__(self, phrase):
        said = []
        trick = tricks.match(phrase, self.loader.poll())
        if trick is None:
            return False, said
        ctx = tricks.TrickCtx(say=said.append, ha=self.ha, kodi_play=self.kodi_play, log=self.log,
                              scripts_dir=self.scripts_dir,
                              confirm=lambda prompt: (said.append("I can't confirm that from the phone.") or False))
        tricks.run(trick, ctx)
        return True, said


def house_command(reflex, command):
    command = ' '.join(str(command or '').split())[:200]
    if not command:
        return 'No command given.'
    matched, said = reflex(command)
    if not matched:
        return f'The house did not recognise "{command}".'
    return ' '.join(said) or 'Done.'


def register(reg, home=None, reflex=None):
    if reflex is not None:
        reg.add('house', 'Send ONE plain command phrase to the house (lights, scenes, media, car climate). '
                         'Returns what the house said. Only phrases the owner has defined work.',
                {'command': str}, lambda command: house_command(reflex, command), writes=True, source='home')
    if home is not None:
        reg.add('house_status', 'Home Assistant READ: locks, doors, temperatures, thermostat. '
                                'query = words to match (empty = locks, doors, climate, temperatures).',
                {'query': str}, lambda query='': house_status(home, query), source='home')
