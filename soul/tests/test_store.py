import json
import os
import time
import unittest

from .helpers import SoulCase

NOW = time.time()


class TestSync(SoulCase):
    def test_ingest_is_idempotent_and_incremental(self):
        s = self.make()
        self.events('2026-03-02', [{'ts': NOW - 100, 'kind': 'heard', 'text': 'Lights on in the kitchen.'},
                                   {'ts': NOW - 90, 'kind': 'said', 'text': 'Done.'}])
        self.assertEqual(s.store.sync(), 2)
        self.assertEqual(s.store.sync(), 0)                      # nothing new
        with open(os.path.join(self.dir, 'events-2026-03-02.jsonl'), 'a') as f:
            f.write(json.dumps({'ts': NOW - 80, 'kind': 'heard', 'text': 'Lights off.'}) + '\n')
        self.assertEqual(s.store.sync(), 1)
        self.assertEqual(s.store.stats()['heard'], 2)

    def test_half_written_last_line_waits(self):
        s = self.make()
        p = self.write('events-2026-03-02.jsonl', json.dumps({'ts': NOW, 'kind': 'heard', 'text': 'First line here.'}) + '\n'
                       + '{"ts": 1, "kind": "heard", "te')
        self.assertEqual(s.store.sync(), 1)
        with open(p, 'a') as f:
            f.write('xt": "Second line here."}\n')
        self.assertEqual(s.store.sync(), 1)

    def test_non_event_kinds_and_garbage_are_skipped(self):
        s = self.make()
        self.events('2026-03-02', [{'ts': NOW, 'kind': 'noise', 'text': 'ignored'}, {'ts': NOW, 'kind': 'heard'}])
        with open(os.path.join(self.dir, 'events-2026-03-02.jsonl'), 'a') as f:
            f.write('not json\n')
        self.assertEqual(s.store.sync(), 0)

    def test_notes_edited_by_hand_are_reconciled(self):
        s = self.make()
        self.write('thoughts.md', '# t\n\n- 03-01 09:00 (reply) first thought about tea\n- 03-01 09:05 (reply) second thought about coffee\n')
        self.assertEqual(s.store.sync(), 2)
        self.write('thoughts.md', '# t\n\n- 03-01 09:05 (reply) second thought about coffee\n')   # first deleted
        s.store.sync()
        texts = [r['text'] for r in s.store.search('thought', kinds=('thought',))]
        self.assertEqual(texts, ['second thought about coffee'])

    def test_memory_forget_only_removes_earlier_facts(self):
        s = self.make()
        mem = [{'ts': 1000.0, 'fact': 'The spare key is in the blue bowl'},
               {'ts': 1001.0, 'forget': 'spare key'},
               {'ts': 1002.0, 'fact': 'The spare key is under the plant pot'}]
        self.write('memory.jsonl', ''.join(json.dumps(m) + '\n' for m in mem))
        s.store.sync()
        live = [r['text'] for r in s.store.search('spare key', kinds=('memory',))]
        self.assertEqual(live, ['The spare key is under the plant pot'])
        self.assertEqual(s.store.search('blue bowl', kinds=('memory',)), [])     # forgotten docs never surface

    def test_diary_day_is_one_doc_and_follows_edits(self):
        s = self.make()
        self.write('diary/2026-03-01.md', '# 2026-03-01\n\nA quiet day with bread.\n')
        s.store.sync()
        self.write('diary/2026-03-01.md', '# 2026-03-01\n\nA quiet day with bread and rain.\n')
        s.store.sync()
        rows = s.store.search('rain', kinds=('diary',))
        self.assertEqual([r['text'] for r in rows], ['A quiet day with bread and rain.'])


class TestSearch(SoulCase):
    def setUp(self):
        super().setUp()
        self.events('2026-03-02', [
            {'ts': NOW - 500, 'kind': 'heard', 'text': 'Set a timer for ninety minutes for the sourdough.'},
            {'ts': NOW - 400, 'kind': 'heard', 'text': 'Play some cello music.'},
            {'ts': NOW - 300, 'kind': 'heard', 'text': 'What is the weather tomorrow?'},
            {'ts': NOW - 200, 'kind': 'heard', 'text': 'Lights on in the living room.'}])

    def test_keyword_only_without_an_embedder(self):
        s = self.make(embedder=False)
        rows = s.search('sourdough timer')
        self.assertEqual(rows[0]['text'], 'Set a timer for ninety minutes for the sourdough.')
        self.assertEqual(s.store.stats()['vectors'], 0)

    def test_hybrid_embeds_and_fuses(self):
        s = self.make(embedder=True)
        rows = s.search('timer for the sourdough')
        self.assertTrue(rows[0]['text'].startswith('Set a timer'))
        self.assertGreater(rows[0]['sim'], 0.5)
        self.assertEqual(s.store.stats()['vectors'], 4)

    def test_wake_word_is_ignored(self):
        s = self.make(embedder=False)
        a = [r['text'] for r in s.search('Computer, cello music')]
        b = [r['text'] for r in s.search('cello music')]
        self.assertEqual(a, b)

    def test_since_until_and_kinds_filter(self):
        s = self.make(embedder=False)
        self.assertEqual(s.search('lights', until=NOW - 250), [])
        self.assertEqual(len(s.search('lights', since=NOW - 250)), 1)
        self.assertEqual(s.search('lights', kinds=('diary',)), [])

    def test_repeats_collapse_to_the_latest(self):
        s = self.make(embedder=False)
        self.events('2026-03-03', [{'ts': NOW - 50, 'kind': 'heard', 'text': 'Play some cello music'},
                                   {'ts': NOW - 40, 'kind': 'heard', 'text': 'Play some cello music.'}])
        rows = [r for r in s.search('cello') if 'cello' in r['text']]
        self.assertEqual(len(rows), 1)

    def test_min_sim_drops_weak_meaning_hits(self):
        s = self.make(embedder=True)
        strong = s.store.search('living room lights', min_sim=0.6)
        self.assertTrue(strong and 'living room' in strong[0]['text'])
        self.assertEqual(s.store.search('zebra crossing', min_sim=0.6), [])

    def test_a_failing_embedder_degrades_to_keyword(self):
        s = self.make(embedder=True)
        s.store.embed_missing()

        def boom(q):
            raise RuntimeError('model gone')
        s.embedder.embed_query = boom
        rows = s.search('cello')
        self.assertTrue(rows and 'cello' in rows[0]['text'])

    def test_vector_cache_picks_up_new_docs(self):
        s = self.make(embedder=True)
        s.search('cello')
        with open(os.path.join(self.dir, 'events-2026-03-02.jsonl'), 'a') as f:
            f.write(json.dumps({'ts': NOW, 'kind': 'heard', 'text': 'Remind me about the tomatoes on the balcony.'}) + '\n')
        rows = s.search('balcony tomatoes')
        self.assertIn('tomatoes', rows[0]['text'])
        self.assertEqual(s.store.stats()['vectors'], 5)


class TestLedger(SoulCase):
    def test_deleting_the_db_loses_nothing(self):
        s = self.make(embedder=True)
        self.write('ideas.md', '# i\n\n- 03-01 04:00 (reflection) Offer a timer by number of minutes.\n')
        s.store.sync()
        idea = s.store.open_ideas()[0]
        s.store.set_idea(idea['id'], 'done', 'works now')
        pid = s.store.propose('Sam bakes bread on Sundays', 0.9, 'evidence', '2026-03-01')
        s.store.propose('Sam likes tea', 0.8, 'e', '2026-03-01')
        s.store.decide(s.store.pending()[1]['id'], False)
        s.store.add_share('2026-03-01', 'You asked for quiet music at six again.')
        sid = s.store.add_share('2026-03-01', 'Do you bake every Sunday?', kind='question')
        s.store.note_spoke('Something I have been wondering: Do you bake every Sunday?', sid)
        os.remove(s.cfg.db_path)
        for ext in ('-wal', '-shm'):
            if os.path.exists(s.cfg.db_path + ext):
                os.remove(s.cfg.db_path + ext)
        t = self.make(embedder=True)
        t.store.sync()
        self.assertEqual(t.store.open_ideas(), [])                                  # closed state survived
        self.assertEqual([p['fact'] for p in t.store.pending()], ['Sam bakes bread on Sundays'])
        self.assertEqual([p['fact'] for p in t.store.pending('rejected')], ['Sam likes tea'])
        self.assertEqual(t.store.questions()[0]['state'], 'asked')
        self.assertIsNotNone(pid)
        self.assertEqual(t.store.spoke_since(0), 1)


class TestFactsQueue(SoulCase):
    def test_exact_and_paraphrase_duplicates_are_not_queued(self):
        s = self.make(embedder=True)
        self.write('profile.md', '# p\n\n- Sam runs on Tuesday and Friday mornings.\n')
        self.assertIsNotNone(s.store.propose('Sam keeps tomatoes on the balcony', 0.9))
        self.assertIsNone(s.store.propose('sam keeps tomatoes on the balcony.', 0.9))             # same text
        self.assertIsNone(s.store.propose('Sam keeps tomatoes on the balcony outside', 0.9))        # near-paraphrase
        self.assertIsNone(s.store.propose('Sam runs on Tuesday and Friday mornings', 0.9))         # already in profile
        self.assertIsNotNone(s.store.propose('Robin drinks oat milk in coffee', 0.9))
        self.assertEqual(len(s.store.pending()), 2)

    def test_empty_fact_is_not_queued(self):
        self.assertIsNone(self.make().store.propose('  . ', 0.9))

    def test_decide_only_acts_on_pending(self):
        s = self.make()
        pid = s.store.propose('Robin drinks oat milk', 0.9)
        self.assertEqual(s.store.decide(pid, True)['fact'], 'Robin drinks oat milk')
        self.assertIsNone(s.store.decide(pid, True))                                               # already decided
        self.assertIsNone(s.store.decide(9999, True))


class TestIdeas(SoulCase):
    def test_similar_idea_matches_any_state(self):
        s = self.make(embedder=True)
        self.write('ideas.md', '# i\n\n- 03-01 04:00 (reflection) Offer a timer that is set by saying only minutes.\n')
        s.store.sync()
        i = s.store.open_ideas()[0]
        s.store.set_idea(i['id'], 'dropped')
        hit = s.store.similar_idea('Offer a timer that is set by saying only minutes.')
        self.assertEqual(hit['state'], 'dropped')
        self.assertIsNone(s.store.similar_idea('Water the balcony tomatoes automatically'))
        self.assertEqual(s.store.open_ideas(), [])


class TestSharesAndQuestions(SoulCase):
    def test_new_share_makes_older_unsaid_ones_stale_unless_keep(self):
        s = self.make()
        a = s.store.add_share('2026-03-01', 'First thing to say.')
        s.store.add_share('2026-03-01', 'Second thing to say.', keep=True)
        c = s.store.add_share('2026-03-02', 'Third thing to say.')
        with s.store.session() as con:
            states = {r['id']: r['state'] for r in con.execute('SELECT id, state FROM shares')}
        self.assertEqual(states[a], 'stale')
        self.assertEqual(states[c], 'waiting')

    def test_waiting_share_is_newest_and_expires_after_two_days(self):
        s = self.make()
        s.store.add_share(time.strftime('%Y-%m-%d', time.localtime(NOW - 4 * 86400)), 'Old news.')
        self.assertIsNone(s.store.waiting_share(now=NOW))
        s.store.add_share(time.strftime('%Y-%m-%d', time.localtime(NOW - 86400)), 'Yesterday news.')
        self.assertEqual(s.store.waiting_share(now=NOW)['text'], 'Yesterday news.')

    def test_questions_dedupe_exact_and_near_verbatim(self):
        s = self.make(embedder=True)
        self.assertIsNotNone(s.store.add_share('2026-03-01', 'Do you bake bread every Sunday?', kind='question'))
        self.assertIsNone(s.store.add_share('2026-03-01', 'do you bake bread every sunday?', kind='question'))
        self.assertIsNone(s.store.add_share('2026-03-01', 'Do you bake bread every Sunday morning?', kind='question'))
        self.assertIsNotNone(s.store.add_share('2026-03-01', 'Which room is warmest in the evening?', kind='question'))

    def test_share_and_question_take_turns_and_one_question_a_day(self):
        s = self.make()
        s.store.add_share(time.strftime('%Y-%m-%d', time.localtime(NOW)), 'A share.')
        s.store.add_share(time.strftime('%Y-%m-%d', time.localtime(NOW)), 'Q one?', kind='question')
        s.store.add_share(time.strftime('%Y-%m-%d', time.localtime(NOW)), 'Q two?', kind='question')
        first = s.store.waiting_share(now=NOW)
        self.assertEqual(first['text'], 'A share.')                              # nothing spoken yet: the share leads
        s.store.note_spoke('A share.', first['id'])
        second = s.store.waiting_share(now=NOW)
        self.assertEqual(second['text'], 'Q one?')                               # then a question
        s.store.note_spoke('Something I have been wondering: Q one?', second['id'])
        self.assertIsNone(s.store.waiting_share(now=NOW))                        # Q two waits for tomorrow

    def test_set_question_only_moves_waiting_or_asked(self):
        s = self.make()
        qid = s.store.add_share('2026-03-01', 'Do you cook for two?', kind='question')
        self.assertEqual(s.store.set_question(qid, 'answered', 'Yes, most nights'), 1)
        self.assertEqual(s.store.set_question(qid, 'dropped'), 0)
        self.assertEqual(s.store.questions()[0]['answer'], 'Yes, most nights')


if __name__ == '__main__':
    unittest.main()
