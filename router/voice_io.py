"""The two things the ladder needs from the outside world: say and ask.

`HttpVoice` talks to a voice daemon that exposes POST /say and POST /ask
(the production setup: wake word + whisper STT + TTS on 127.0.0.1:7781).
`ConsoleVoice` prints and reads stdin, so the ladder runs in a terminal
with no microphone at all.

An ask returns {heard, yes, unclear}: `yes` is True/False/None for mode
"yesno", and `unclear` is set when speech was heard but could not be
resolved confidently. A write needs yes=True AND not unclear.
"""
import json
import os
import re
import urllib.request

YES_RE = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|go ahead|do it|mm-?hmm|uh-huh)\b", re.I)
NO_RE = re.compile(r"^\W*(no|nope|don'?t|stop|cancel|never ?mind)\b", re.I)


class ConsoleVoice:
    def say(self, text, earcon=None):
        if text:
            print(f'  [say] {text}', flush=True)

    def ask(self, text, mode='text', timeout_s=8):
        try:
            heard = input(f'  [ask] {text} > ').strip()
        except EOFError:
            heard = ''
        if not heard:
            return {'heard': None, 'yes': None, 'unclear': False}
        yes = True if YES_RE.match(heard) else False if NO_RE.match(heard) else None
        return {'heard': heard, 'yes': yes if mode == 'yesno' else None,
                'unclear': mode == 'yesno' and yes is None}


class HttpVoice:
    def __init__(self, base=None):
        self.base = (base or os.environ.get('VOICE_URL') or 'http://127.0.0.1:7781').rstrip('/')

    def _post(self, path, payload, timeout):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or '{}')
        except Exception:
            return None

    def say(self, text, earcon=None):
        if not text:
            return None
        p = {'text': text}
        if earcon:
            p['earcon'] = earcon
        return self._post('/say', p, timeout=30)

    def ask(self, text, mode='text', timeout_s=8):
        return self._post('/ask', {'text': text, 'mode': mode, 'timeout_s': timeout_s},
                          timeout=timeout_s + 25) or {}
