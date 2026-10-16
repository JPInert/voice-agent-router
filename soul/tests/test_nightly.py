"""The nightly job with a scripted model reply: what is applied, what is refused, and the order."""
import datetime as dt
import json
import os
import time
import unittest

from soul.demo import load_fixture, materialise

from .helpers import FakeClient, SoulCase

YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def good(**over):
    d = {'diary': 'A quiet day. Sam asked for the weather and put the living room lights on.',
         'thoughts': ['Sam asks about the weather most mornings.', 'The hall lights get asked for second.'],
         'ideas': ['Let the house understand "the hall too" after a lights command.',
                   'Offer timers that can be set by saying only a number of minutes'],     # reworded copy of a known idea
         'idea_updates': [],
         'facts': [{'fact': 'Sam checks the weather every morning.', 'confidence': 0.9, 'evidence': '08:15 heard'},
                   {'fact': 'Sam might like tea.', 'confidence': 0.4, 'evidence': 'x'},
                   {'fact': 'Sam runs on Tuesday and Friday mornings.', 'confidence': 0.95, 'evidence': 'y'}],
         'shares': ['the best one.', 'second best.', 'third best.'],              # the prompt asks for best first
         'questions': ['Do you bake every Sunday?'],
         'question_updates': []}
    d.update(over)
    return d


class TestNightly(SoulCase):
    def setUp(self):
        super().setUp()
        self.fx = load_fixture()
        materialise(self.dir, fx=self.fx)

    def run_night(self, reply, **kw):
        client = FakeClient([reply] if not isinstance(reply, list) else reply)
        s = self.make(client=client, embedder=True, user_name='Sam', **kw)
        return s, client

    def test_applies_a_good_reply(self):
        s, client = self.run_night(good())
        d = s.nightly(YESTERDAY)
        self.assertTrue(d['diary'].startswith('A quiet day'))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]['model'], s.cfg.nightly_model)
        thoughts = self.read('thoughts.md')
        self.assertIn(f'(reflection {YESTERDAY[5:]}) Sam asks about the weather most mornings.', thoughts)
        ideas = self.read('ideas.md')
        self.assertIn('the hall too', ideas)                                       # new idea kept
        self.assertEqual(ideas.count('number of minutes'), 1)                      # the repeat of a known idea dropped
        self.assertIn('A quiet day', self.read('diary', f'{YESTERDAY}.md'))

    def test_facts_are_queued_never_saved(self):
        s, _ = self.run_night(good())
        s.nightly(YESTERDAY)
        queued = [p['fact'] for p in s.pending()]
        self.assertEqual(queued, ['Sam checks the weather every morning'])         # 0.4 dropped, profile dupe dropped
        mem = self.read('memory.jsonl')
        self.assertNotIn('weather every morning', mem)                             # nothing saved before a yes
        self.assertEqual(s.approve('all'), ['Sam checks the weather every morning'])
        self.assertIn('weather every morning', self.read('memory.jsonl'))
        self.assertEqual(s.pending(), [])

    def test_rejected_fact_is_shown_back_as_a_no_and_not_requeued(self):
        s, client = self.run_night(good())
        s.nightly(YESTERDAY)
        s.reject('all')
        client.calls.clear()
        s.nightly(YESTERDAY, force=True)
        self.assertEqual(s.pending(), [])                                          # the same fact is not proposed again
        body = client.calls[0]['user']
        self.assertIn('Sam checks the weather every morning (they said NO', body)

    def test_idea_close_only_for_ids_it_was_shown(self):
        s, client = self.run_night(good())
        with s.store.session() as c:
            s.store.sync(c)
            real = s.store.open_ideas(c=c)[0]['id']
        client.replies = [good(ideas=[], idea_updates=[{'id': real, 'state': 'done', 'why': 'works'},
                                                       {'id': 999, 'state': 'done', 'why': 'invented id'},
                                                       {'id': real, 'state': 'bogus'}])]
        s.nightly(YESTERDAY)
        self.assertEqual(s.store.open_ideas(), [])
        self.assertIn(f'- id {real} (', client.calls[0]['user'])                    # it was shown with its id

    def test_shares_best_first_and_questions_queued(self):
        s, _ = self.run_night(good())
        s.nightly(YESTERDAY)
        self.assertEqual(s.store.waiting_share()['text'], 'the best one.')
        self.assertEqual([q['text'] for q in s.store.questions()], ['Do you bake every Sunday?'])

    def test_question_updates_only_for_shown_ids(self):
        s, client = self.run_night(good())
        s.nightly(YESTERDAY)
        qid = s.store.questions()[0]['id']
        client.replies = [good(shares=[], questions=[], ideas=[], facts=[],
                               question_updates=[{'id': qid, 'state': 'answered', 'answer': 'Most Sundays'},
                                                 {'id': 999, 'state': 'answered', 'answer': 'invented'}])]
        s.nightly(YESTERDAY, force=True)
        self.assertEqual((s.store.questions()[0]['state'], s.store.questions()[0]['answer']), ('answered', 'Most Sundays'))
        self.assertIn(f'id {qid} waiting: Do you bake every Sunday?', client.calls[-1]['user'])

    def test_diary_exists_means_done(self):
        s, client = self.run_night(good())
        self.assertIsNotNone(s.nightly(YESTERDAY))
        self.assertIsNone(s.nightly(YESTERDAY))                                     # a re-run must not double the thoughts
        self.assertEqual(len(client.calls), 1)
        self.assertIsNotNone(s.nightly(YESTERDAY, force=True))
        self.assertEqual(len(client.calls), 2)

    def test_unusable_reply_raises_after_one_retry_and_writes_no_diary(self):
        s, client = self.run_night(['not json', '{"thoughts": ["no diary key"]}'])
        with self.assertRaises(RuntimeError):
            s.nightly(YESTERDAY)
        self.assertEqual(len(client.calls), 2)                                      # one retry, with the nudge
        self.assertIn('not valid JSON', client.calls[1]['system'])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'diary', f'{YESTERDAY}.md')))
        self.assertNotIn('no diary key', self.read('thoughts.md'))

    def test_retry_that_succeeds(self):
        s, client = self.run_night(['{"diary": "broken', json.dumps(good())])
        self.assertIsNotNone(s.nightly(YESTERDAY))
        self.assertEqual(len(client.calls), 2)

    def test_empty_diary_counts_as_unusable(self):
        s, _ = self.run_night(good(diary='   '))
        with self.assertRaises(RuntimeError):
            s.nightly(YESTERDAY)

    def test_a_day_with_no_events_makes_no_model_call(self):
        s, client = self.run_night(good())
        self.assertIsNone(s.nightly('2020-01-01'))
        self.assertEqual(client.calls, [])

    def test_bad_field_types_do_not_crash(self):
        s, _ = self.run_night({'diary': 'ok', 'thoughts': 'a single string', 'ideas': None, 'facts': ['not a dict', {'fact': 'x', 'confidence': 'high'}],
                               'shares': 'not a list', 'questions': [5, ''], 'idea_updates': ['x'], 'question_updates': [None]})
        d = s.nightly(YESTERDAY)
        self.assertEqual(d['thoughts'], ['a single string'])
        self.assertEqual(s.pending(), [])
        self.assertEqual(s.store.waiting_share(), None)

    def test_material_sections_reach_the_model(self):
        s, client = self.run_night(good())
        s.nb.append_jsonl('verdicts.jsonl', {'ts': time.mktime(time.strptime(YESTERDAY, '%Y-%m-%d')) + 3600,
                                             'text': 'The hall too.', 'gap': 'Understand "the hall too" as a follow-up'})
        s.nightly(YESTERDAY)
        body = client.calls[0]['user']
        for section in ('PROFILE:', 'WHAT YOU ALREADY KNOW:', f'EVENTS {YESTERDAY}:', 'GAPS:', 'REPEATED REQUESTS:',
                        'FROM BEFORE:', 'OPEN IDEAS:', 'YOUR QUESTIONS:'):
            self.assertIn(section, body)
        self.assertIn('Understand "the hall too" as a follow-up', body)
        self.assertIn('heard: The hall too.', body)
        self.assertIn('The spare key lives in the blue bowl', body)                 # a confirmed memory
        self.assertIn('serves Sam', client.calls[0]['system'])
        self.assertNotIn('%(', client.calls[0]['system'])

    def test_gap_ledger_and_repeats_become_ideas(self):
        self.events(YESTERDAY, [{'ts': time.mktime(time.strptime(YESTERDAY, '%Y-%m-%d')) + 36000, 'kind': 'heard', 'text': 'Play some folk.'},
                                {'ts': time.mktime(time.strptime(YESTERDAY, '%Y-%m-%d')) + 36060, 'kind': 'heard', 'text': 'Play some folk'}])
        s, _ = self.run_night(good(ideas=[]))
        s.nb.append_jsonl('verdicts.jsonl', {'ts': time.mktime(time.strptime(YESTERDAY, '%Y-%m-%d')) + 40000,
                                             'text': 'Set a timer', 'gap': 'Kitchen timers by voice'})
        s.nightly(YESTERDAY)
        ideas = self.read('ideas.md')
        self.assertIn('(gap ' + YESTERDAY[5:] + ') Kitchen timers by voice', ideas)
        self.assertIn('Same request twice in a row: ', ideas)

    def test_works_without_an_embedder(self):
        client = FakeClient([good()])
        s = self.make(client=client, embedder=False)
        d = s.nightly(YESTERDAY)
        self.assertIsNotNone(d)                                                     # no vectors: no dedupe, but it runs
        self.assertEqual(len(s.pending()), 2)                                       # the profile dupe is not caught without vectors


if __name__ == '__main__':
    unittest.main()
