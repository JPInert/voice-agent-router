"""SentenceSpeaker regression tests.

`BrokenSpeaker` is the original feed() loop, verbatim, kept so the test can
prove it detects the wedge. Any reply whose first streamed sentence was shorter
than min_chars ("OK. Let me check...") made it spin forever.
"""
import multiprocessing
import random
import threading

from router.speaker import SENT_END, SentenceSpeaker


class BrokenSpeaker:
    def __init__(self, min_chars=25):
        self.buf = ''
        self.min_chars = min_chars
        self.spoken = []

    def feed(self, text):
        self.buf += text
        while True:
            parts = SENT_END.split(self.buf, maxsplit=1)
            if len(parts) < 2:
                break
            sent, self.buf = parts[0].strip(), parts[1]
            if len(sent) >= self.min_chars:
                self.spoken.append(sent)
            else:
                self.buf = sent + ' ' + self.buf     # restores its input: the spin


def _returns_within(fn, seconds=1.0):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive()


def _feed_broken():
    BrokenSpeaker().feed('OK. Let me check the printer queue now. ')


def test_broken_version_wedges():
    # the instrument check: the tests below would be meaningless if this returned.
    # A separate process, so the spin can be killed instead of eating a core for the run.
    p = multiprocessing.Process(target=_feed_broken, daemon=True)
    p.start()
    p.join(1.0)
    wedged = p.is_alive()
    p.terminate()
    assert wedged


def test_short_first_sentence_returns():
    out = []
    s = SentenceSpeaker(say=out.append)
    assert _returns_within(lambda: s.feed('OK. Let me check the printer queue now. '))
    s.flush()
    assert out == ['OK. Let me check the printer queue now.']


def test_fuzzed_streams_never_wedge_or_lose_text():
    rng = random.Random(916)
    words = ['OK.', 'Yes.', 'Done!', 'Right?', 'the', 'printer', 'queue', 'is', 'empty',
             'and', 'volume', 'is', 'forty', 'percent.', 'Hmm.', 'Checking', 'now.']
    for _ in range(500):
        text = ' '.join(rng.choice(words) for _ in range(rng.randint(1, 40)))
        out = []
        s = SentenceSpeaker(say=out.append)

        def run():
            i = 0
            while i < len(text):
                n = rng.randint(1, 12)
                s.feed(text[i:i + n])
                i += n
            s.flush()

        assert _returns_within(run, 2.0), text
        assert ' '.join(out).split() == text.split()
