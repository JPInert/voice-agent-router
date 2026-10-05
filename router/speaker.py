"""Speak a streaming model reply sentence by sentence.

The first sentence is out loud while the rest is still generating. This class
is also the subject of the worst production bug this project has had: see
"The three-day lock-up" in the README, and tests/test_speaker.py, which keeps
the broken version around to prove the regression test catches it.
"""
import re

SENT_END = re.compile(r'(?<=[.!?])\s+')


def speakable(text, limit=400):
    """Model prose -> something worth hearing: no fences, no bullets, no markup."""
    t = re.sub(r'```.*?```', ' ', text or '', flags=re.S)
    t = re.sub(r'^\s*[-*\d.]+\s+', '', t, flags=re.M)
    t = re.sub(r'[`*_#>|]+', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:limit]


class SentenceSpeaker:
    """Buffer streamed text, emit whole sentences of at least `min_chars`.

    Short sentences ("OK.") are merged into the next one so the TTS never
    speaks a two-word fragment on its own.
    """

    def __init__(self, say, enabled=True, min_chars=25, clean=None):
        self.say = say
        self.clean = clean         # optional: e.g. code -> words, before the markup strip
        self.buf = ''
        self.pending = ''          # short sentences held until the merge is long enough
        self.enabled = enabled
        self.min_chars = min_chars
        self.spoken = []

    def feed(self, text):
        # Each pass must CONSUME one boundary. The first version put a short
        # sentence back into buf (`self.buf = sent + ' ' + self.buf`), which
        # re-creates the same boundary byte for byte and spins forever. That ran
        # on the asyncio loop thread, so the service stayed "healthy" while
        # nothing was ever scheduled again. Short ones wait in `pending` instead,
        # so the buffer strictly shrinks.
        self.buf += text
        while True:
            parts = SENT_END.split(self.buf, maxsplit=1)
            if len(parts) < 2:
                break
            sent, self.buf = parts[0].strip(), parts[1]
            sent = f'{self.pending} {sent}'.strip() if self.pending else sent
            if len(sent) >= self.min_chars:
                self.pending = ''
                self._emit(sent)
            else:
                self.pending = sent

    def flush(self):
        rest = f'{self.pending} {self.buf}'.strip()
        self.buf = self.pending = ''
        if rest:
            self._emit(rest)

    def _emit(self, sent):
        if self.clean:
            sent = self.clean(sent)
        clean = re.sub(r'[`*_#>|]+', '', sent).strip()
        if not clean:
            return
        self.spoken.append(clean)
        if self.enabled:
            self.say(clean)
