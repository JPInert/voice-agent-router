import json
import os
import unittest

from .helpers import SoulCase


class TestApproval(SoulCase):
    def setUp(self):
        super().setUp()
        self.s = self.make(embedder=True)
        self.a = self.s.store.propose('Robin drinks oat milk in coffee', 0.9, '08:00 line', '2026-03-01')
        self.b = self.s.store.propose('Sam keeps tomatoes on the balcony', 0.8, '17:45 line', '2026-03-01')

    def memory(self):
        p = os.path.join(self.dir, 'memory.jsonl')
        return [json.loads(l) for l in self.read('memory.jsonl').splitlines()] if os.path.exists(p) else []

    def test_nothing_is_saved_until_approved(self):
        self.assertEqual(self.memory(), [])
        self.assertEqual(len(self.s.pending()), 2)

    def test_approve_one_writes_memory_with_source(self):
        self.assertEqual(self.s.approve(str(self.a)), ['Robin drinks oat milk in coffee'])
        rows = self.memory()
        self.assertEqual((rows[0]['fact'], rows[0]['source']), ('Robin drinks oat milk in coffee', 'nightly'))
        self.assertEqual([p['id'] for p in self.s.pending()], [self.b])

    def test_approve_all_and_reject_all(self):
        self.assertEqual(len(self.s.approve('all')), 2)
        self.assertEqual(self.s.pending(), [])
        self.assertEqual(self.s.approve('all'), [])                                # idempotent

    def test_reject_writes_nothing_and_is_remembered_as_no(self):
        self.assertEqual(self.s.reject('all'), ['Robin drinks oat milk in coffee', 'Sam keeps tomatoes on the balcony'])
        self.assertEqual(self.memory(), [])
        self.assertEqual(len(self.s.store.pending('rejected')), 2)
        self.assertIsNone(self.s.store.propose('Robin drinks oat milk in coffee', 0.9))     # never re-proposed

    def test_unknown_id_is_a_no_op(self):
        self.assertEqual(self.s.approve('999'), [])
        self.assertEqual(self.s.reject('999'), [])

    def test_non_numeric_id_is_a_clear_error_and_changes_nothing(self):
        for bad in ('foo', '1x', '', '-1', '1.5'):
            with self.assertRaisesRegex(ValueError, 'expected a pending id'):
                self.s.approve(bad)
            with self.assertRaisesRegex(ValueError, 'expected a pending id'):
                self.s.reject(bad)
        self.assertEqual(len(self.s.pending()), 2)

    def test_approved_fact_shows_up_in_context_and_is_not_requeued(self):
        self.s.approve(str(self.a))
        self.assertIn('Robin drinks oat milk', self.s.context())
        self.assertIsNone(self.s.store.propose('Robin drinks oat milk in coffee', 0.9))

    def test_learn_walks_the_queue_with_yes_no_and_stops_on_silence(self):
        asked = []
        answers = iter([True, None])

        def ask(q):
            asked.append(q)
            return next(answers)
        kept, dropped, left = self.s.learn(ask)
        self.assertEqual((kept, dropped, left), (1, 0, 1))                         # the second was not answered: stays queued
        self.assertTrue(asked[0].startswith('Here is one. Robin drinks oat milk in coffee.'))
        self.assertTrue(asked[0].endswith('Shall I keep that?'))
        self.assertEqual(len(asked), 2)

    def test_learn_no_drops_it(self):
        self.assertEqual(self.s.learn(lambda q: False), (0, 2, 0))
        self.assertEqual(self.memory(), [])

    def test_learn_respects_the_limit(self):
        self.s.store.propose('Sam practises cello most evenings', 0.8)
        self.s.store.propose('Robin walks to work on Fridays', 0.8)
        kept, dropped, left = self.s.learn(lambda q: True, limit=3)
        self.assertEqual((kept, left), (3, 1))


if __name__ == '__main__':
    unittest.main()
