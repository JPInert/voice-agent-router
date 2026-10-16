"""The screen's PLUMBING, with a scripted model. These tests prove what the code does with a verdict
(silence when unsure, fallback on failure or timeout, nothing spoken that looks like code, thoughts and
gaps written down). They do NOT measure whether a real model judges chatter correctly: that is
tools/screen_eval.py, which needs an API key and is not part of the offline suite."""
import ast
import json
import os
import time
import unittest

from soul.demo import load_fixture, materialise

from .helpers import FakeClient, SoulCase

CAPS = ['"lights on", "lights off", "lights 40"', '"play <artist> music"']


def scripted(fx):
    """Answer each fixture case the way a correct screen would, keyed on the NOW line."""
    by_now = {c['now']: c for c in fx['screen_cases']}

    def reply(system, user, model):
        now = user.split('NOW: ', 1)[1].split("  (failed at")[0]
        c = by_now[ast.literal_eval(now)]           # NOW is rendered with repr()
        if c['expect'] == 'speak':
            return json.dumps({'addressed': True, 'conf': 0.9, 'intent': 'test', 'reply': f"Reply to: {c['now']}",
                               'gap': '', 'thought': ''})
        return json.dumps({'addressed': False, 'conf': 0.9, 'intent': '', 'reply': '', 'gap': '',
                           'thought': 'Sam talks to Robin about the bins in the evening.'})
    return reply


class TestScreen(SoulCase):
    def test_fixture_cases_speak_or_stay_silent(self):
        fx = load_fixture()
        s = self.make(client=FakeClient(scripted(fx)), embedder=True, capabilities=CAPS, user_name='Sam')
        materialise(self.dir, fx=fx)
        for c in fx['screen_cases']:
            v = s.screen(c['now'], stage=c['stage'], fallback='FALLBACK')
            if c['expect'] == 'speak':
                self.assertTrue(v.addressed and v.speak.startswith('Reply to:'), c['now'])
            else:
                self.assertFalse(v.addressed, c['now'])
                self.assertEqual(v.speak, 'FALLBACK', c['now'])

    def test_prompt_carries_config_context_and_recent(self):
        fx = load_fixture()
        client = FakeClient(['{"addressed": false}'])
        s = self.make(client=client, embedder=True, capabilities=CAPS, user_name='Sam', wake_word='assistant',
                      spoken_style='STYLE-LINE', screen_model='model-x')
        materialise(self.dir, fx=fx)
        now = time.time()
        s.observe({'ts': now - 20, 'kind': 'heard', 'text': 'Lights on in the living room.'})
        s.observe({'ts': now - 19, 'kind': 'routed', 'what': 'lights', 'text': 'living room lights on'})
        s.observe({'ts': now - 5000, 'kind': 'heard', 'text': 'Too old to matter.'})
        s.screen('The hall too.', stage='unclear')
        call = client.calls[0]
        self.assertEqual(call['model'], 'model-x')
        sysp = call['system']
        for needle in ('wake word "assistant"', 'requests from Sam', '"play <artist> music"', 'YOU CANNOT ACT',
                       'STYLE-LINE', 'Sam lives with Robin', 'Mochi'):
            self.assertIn(needle, sysp)
        self.assertNotIn('%(', sysp)                                   # every placeholder was filled
        self.assertIn('heard: Lights on in the living room.', call['user'])
        self.assertIn('house did (lights): living room lights on', call['user'])
        self.assertNotIn('Too old to matter', call['user'])           # outside the recent window
        self.assertIn("NOW: 'The hall too.'  (failed at: unclear)", call['user'])

    def test_unsure_means_silence_even_with_junk_types(self):
        for raw in ('{"addressed": "false"}', '{"addressed": "yes"}', '{"addressed": 1}', '{}', 'not json at all', ''):
            s = self.make(client=FakeClient([raw]))
            v = s.screen('Whatever.', fallback='FB')
            self.assertFalse(v.addressed, raw)
            self.assertEqual(v.speak, 'FB')

    def test_a_failed_call_speaks_the_fallback(self):
        s = self.make(client=FakeClient([RuntimeError('network down')]))
        v = s.screen('Set a timer.', fallback='Sorry, I missed that.')
        self.assertTrue(v.failed)
        self.assertEqual(v.speak, 'Sorry, I missed that.')

    def test_timeout_speaks_the_fallback(self):
        def slow(system, user, model):
            time.sleep(0.6)
            return json.dumps({'addressed': True, 'reply': 'Too late.'})
        s = self.make(client=FakeClient(slow), screen_timeout_s=0.1)
        t = time.time()
        v = s.screen('Set a timer.', fallback='FB')
        self.assertLess(time.time() - t, 0.5)
        self.assertTrue(v.failed)
        self.assertEqual(v.speak, 'FB')

    def test_replies_are_made_speakable(self):
        s = self.make(client=FakeClient([{'addressed': True, 'reply': 'Run `sudo systemctl restart foo` in /etc/foo/bar.conf now.'}]))
        v = s.screen('Fix it.', fallback='FB')
        self.assertTrue(v.addressed)
        self.assertNotIn('`', v.reply)
        self.assertNotIn('/etc', v.reply)
        s = self.make(client=FakeClient([{'addressed': True, 'reply': '```rm -rf / ; {}```'}]))
        v = s.screen('Fix it.', fallback='FB')
        self.assertEqual((v.reply, v.speak), ('', 'FB'))               # nothing speakable left: the fallback line

    def test_addressed_with_empty_reply_and_no_fallback_is_silence(self):
        s = self.make(client=FakeClient([{'addressed': True, 'reply': ''}]))
        self.assertEqual(s.screen('Hm.').speak, '')

    def test_thought_gap_and_ledger_are_written(self):
        s = self.make(client=FakeClient([{'addressed': True, 'conf': 0.8, 'intent': 'set timer', 'reply': 'I cannot set timers yet.',
                                          'gap': 'Set a kitchen timer by voice', 'thought': 'Sam bakes on Sundays.'}]))
        s.screen('Set a timer for ninety minutes.', stage='unclear')
        self.assertIn('Sam bakes on Sundays.', self.read('thoughts.md'))
        self.assertIn('(offer) Set a kitchen timer by voice', self.read('ideas.md'))
        rows = [json.loads(l) for l in self.read('verdicts.jsonl').splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['addressed'], rows[0]['gap'], rows[0]['text']),
                         (True, 'Set a kitchen timer by voice', 'Set a timer for ninety minutes.'))

    def test_unaddressed_gap_is_ignored_but_thought_is_kept(self):
        s = self.make(client=FakeClient([{'addressed': False, 'gap': 'should be ignored', 'thought': 'A pattern.'}]))
        s.screen('Talking to someone else.')
        self.assertNotIn('should be ignored', self.read('ideas.md'))
        self.assertIn('A pattern.', self.read('thoughts.md'))

    def test_observe_writes_events_and_only_known_kinds(self):
        s = self.make()
        s.observe({'ts': time.time(), 'kind': 'heard', 'text': 'Hello there.'})
        s.observe({'ts': time.time(), 'kind': 'status', 'text': 'ignored'})
        day = time.strftime('%Y-%m-%d')
        lines = self.read(f'events-{day}.jsonl').splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['text'], 'Hello there.')


if __name__ == '__main__':
    unittest.main()
