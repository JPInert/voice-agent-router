"""The eval and bench tools, run with fakes so no model or download is needed."""
import json
import unittest

from soul.demo import load_fixture
from soul.tools import bench, screen_eval

from .helpers import FakeClient
from .test_screen import scripted


class TestScreenEval(unittest.TestCase):
    def test_a_correct_screen_scores_everything(self):
        fx = load_fixture()
        lines = []
        p, t, chatter = screen_eval.run(FakeClient(scripted(fx)), runs=2, out=lines.append, fx=fx)
        self.assertEqual((p, t, chatter), (len(fx['screen_cases']) * 2,) * 2 + (0,))
        self.assertIn('0', lines[-1].split('spoke:')[1])

    def test_a_chatty_screen_is_caught(self):
        fx = load_fixture()
        always = FakeClient([json.dumps({'addressed': True, 'conf': 0.9, 'reply': 'Hello!'})])
        p, t, chatter = screen_eval.run(always, out=lambda *_: None, fx=fx)
        n_silent = sum(c['expect'] == 'silent' for c in fx['screen_cases'])
        self.assertEqual(chatter, n_silent)
        self.assertEqual(t - p, n_silent)

    def test_recent_lines_reach_the_model(self):
        fx = load_fixture()
        client = FakeClient(['{"addressed": false}'])
        screen_eval.run(client, out=lambda *_: None, fx={**fx, 'screen_cases': fx['screen_cases'][:1]})
        self.assertIn('house did (lights): living room lights on', client.calls[0]['user'])
        self.assertIn('"lights 40"', client.calls[0]['system'])


class TestBench(unittest.TestCase):
    def test_corpus_is_deterministic_and_synthetic(self):
        a, b = bench.corpus(50), bench.corpus(50)
        self.assertEqual([r['text'] for r in a], [r['text'] for r in b])
        self.assertEqual(len(a), 50)
        self.assertTrue(all(r['kind'] in ('heard', 'said', 'routed') for r in a))


if __name__ == '__main__':
    unittest.main()
