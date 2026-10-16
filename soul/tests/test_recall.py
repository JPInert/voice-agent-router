import datetime as dt
import time
import unittest

from soul.demo import load_fixture, materialise

from .helpers import FakeClient, SoulCase


class TestContext(SoulCase):
    def setUp(self):
        super().setUp()
        materialise(self.dir)

    def test_context_has_persona_profile_memory_diary_thoughts(self):
        s = self.make(user_name='Sam')
        s.nb.ensure()
        ctx = s.context()
        for needle in ('WHO YOU ARE:', 'ABOUT SAM (their profile):', 'Sam lives with Robin',
                       'THINGS THEY ASKED YOU TO REMEMBER:', 'blue bowl', 'DIARY ', 'YOUR RECENT THOUGHTS:'):
            self.assertIn(needle, ctx)
        self.assertNotIn('FROM BEFORE', ctx)                                       # no query, no recall block

    def test_query_adds_from_before_with_labelled_kinds(self):
        s = self.make(embedder=True)
        ctx = s.context(query='play some cello music again')
        self.assertIn('FROM BEFORE', ctx)
        self.assertIn('they said: Play some cello music.', ctx)

    def test_from_before_excludes_the_last_five_minutes(self):
        s = self.make(embedder=True)
        s.observe({'ts': time.time() - 30, 'kind': 'heard', 'text': 'Play some cello music right now.'})
        ctx = s.context(query='cello music')
        self.assertNotIn('right now', ctx)                                         # that is RECENT's job, not memory's

    def test_context_survives_a_broken_search(self):
        s = self.make(embedder=True)
        s.store.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db locked'))
        self.assertIn('WHO YOU ARE', s.context(query='anything'))

    def test_caps_keep_the_prompt_bounded(self):
        s = self.make()
        s.nb.ensure()
        s.nb.append_jsonl('memory.jsonl', {'ts': 1.0, 'fact': 'x'})
        with open(s.nb.p('profile.md'), 'a') as f:
            f.write('- ' + 'long ' * 2000 + '\n')
        self.assertLess(len(s.context()), 4000)


class TestRecallMaterial(SoulCase):
    def setUp(self):
        super().setUp()
        self.fx = load_fixture()
        materialise(self.dir, fx=self.fx)

    def test_named_day_returns_the_whole_day(self):
        s = self.make(embedder=True)
        lines = s.recall_material('what happened yesterday')
        self.assertTrue(any('Did you feed Mochi already?' in l for l in lines))
        self.assertTrue(any('The hall too.' in l for l in lines))

    def test_matching_lines_diary_and_saved_facts(self):
        s = self.make(embedder=True)
        lines = s.recall_material('what did I ask about the timer')
        joined = '\n'.join(lines)
        self.assertIn('ninety minutes', joined)
        self.assertIn('diary ', joined)
        self.assertIn('they told you to remember: The spare key lives in the blue bowl', joined)   # facts always ride along

    def test_own_thoughts_on_the_subject_are_included(self):
        s = self.make(embedder=True)
        joined = '\n'.join(s.recall_material('when does the cello come out'))
        self.assertIn('you thought: Sam asks for quiet music around the time the cello comes out.', joined)

    def test_oldest_first_and_limited(self):
        s = self.make(embedder=True)
        lines = s.recall_material('what happened yesterday', limit=3)
        self.assertEqual(len([l for l in lines if ' they said:' in l or ' you said:' in l or ' you did:' in l]), 3)

    def test_falls_back_to_word_overlap_when_search_is_down(self):
        s = self.make(embedder=False)
        s.store.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db locked'))
        joined = '\n'.join(s.recall_material('anything about timer'))
        self.assertIn('ninety minutes', joined)

    def test_forgotten_facts_do_not_come_back(self):
        s = self.make(embedder=True)
        s.nb.forget('spare key')
        self.assertNotIn('blue bowl', '\n'.join(s.recall_material('where is the spare key')))


class TestAnswer(SoulCase):
    def setUp(self):
        super().setUp()
        materialise(self.dir)

    def test_answer_sends_memory_and_returns_speakable_text(self):
        client = FakeClient(['You asked twice on Sunday morning. See /home/x/y/z.txt for more.'])
        s = self.make(client=client, embedder=True, user_name='Sam', spoken_style='STYLE-LINE', recall_model='m-r')
        out = s.answer('did I ask about a timer?')
        self.assertTrue(out.startswith('You asked twice'))
        self.assertNotIn('/home', out)
        call = client.calls[0]
        self.assertEqual(call['model'], 'm-r')
        self.assertIn('MEMORY:', call['user'])
        self.assertIn('ninety minutes', call['user'])
        self.assertIn('THEY ASKED: did I ask about a timer?', call['user'])
        self.assertIn('Sam just asked you to remember', call['system'])
        self.assertIn('STYLE-LINE', call['system'])

    def test_failure_is_a_plain_apology(self):
        s = self.make(client=FakeClient([RuntimeError('down')]))
        self.assertEqual(s.answer('do you remember the key?'), "I can't reach my memory right now.")


if __name__ == '__main__':
    unittest.main()
