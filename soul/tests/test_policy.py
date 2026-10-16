import os
import time
import unittest

from soul import policy

from .helpers import SoulCase


def at(hour, minute=0, days=0):
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, hour, minute, 0, 0, 0, -1))


class TestShareDue(SoulCase):
    def setUp(self):
        super().setUp()
        self.s = self.make(share_per_day=[2, 2])
        self.day = time.strftime('%Y-%m-%d')
        self.s.store.add_share(self.day, 'You asked for quiet music at six again.')

    def due(self, now, talk=None, **kw):
        return self.s.share_due(last_talk_ts=(now - 60) if talk is None else talk, now=now, **kw)

    def spoke(self, ts, text='earlier line'):
        with self.s.store.session() as c, c:
            c.execute('INSERT INTO spoke(ts, text) VALUES (?,?)', (ts, text))

    def test_speaks_inside_hours_right_after_a_conversation(self):
        got = self.due(at(11))
        self.assertEqual(got[1], 'You asked for quiet music at six again.')

    def test_never_outside_the_hours_window(self):
        for h, m in ((8, 59), (21, 0), (23, 30), (3, 0)):
            self.assertIsNone(self.due(at(h, m)), (h, m))
        self.assertIsNotNone(self.due(at(9, 0)))
        self.assertIsNotNone(self.due(at(20, 59)))

    def test_only_while_a_conversation_is_warm(self):
        now = at(11)
        self.assertIsNone(self.s.share_due(last_talk_ts=None, now=now))
        self.assertIsNone(self.s.share_due(last_talk_ts=now - 601, now=now))
        self.assertIsNotNone(self.s.share_due(last_talk_ts=now - 599, now=now))

    def test_presence_hook_can_veto(self):
        home = {'v': False}
        s = self.make(share_per_day=[2, 2], presence=lambda: home['v'])
        s.store.add_share(self.day, 'Something kind.')
        now = at(11)
        self.assertIsNone(s.share_due(last_talk_ts=now - 10, now=now))
        home['v'] = True
        self.assertIsNotNone(s.share_due(last_talk_ts=now - 10, now=now))

    def test_gap_between_lines(self):
        now = at(15)
        self.spoke(now - 3600)                                  # one hour ago, gap is two
        self.assertIsNone(self.due(now))
        self.s.cfg.share_gap_s = 1800                           # a shorter gap: the same line is now allowed
        self.assertIsNotNone(self.due(now))

    def test_daily_quota(self):
        now = at(17)
        self.spoke(at(9, 30))
        self.assertIsNotNone(self.due(now))                     # one of two used
        self.spoke(at(12, 0))
        self.assertIsNone(self.due(now))                        # two of two used

    def test_yesterday_does_not_count_against_today(self):
        self.spoke(at(10, 0, days=-1))
        self.spoke(at(15, 0, days=-1))
        self.assertIsNotNone(self.due(at(11)))

    def test_nothing_waiting_means_silence(self):
        s = self.make(share_per_day=[2, 2], data_dir=self.dir + '/other')    # a fresh, empty notebook
        self.assertIsNone(s.share_due(last_talk_ts=at(11) - 5, now=at(11)))

    def test_a_question_comes_with_its_prefix(self):
        s = self.make(share_per_day=[2, 2], data_dir=self.dir + '/other')
        s.store.add_share(self.day, 'Do you cook for two?', kind='question')
        _, text = s.share_due(last_talk_ts=at(11) - 5, now=at(11))
        self.assertEqual(text, "Something I've been wondering: Do you cook for two?")

    def test_share_said_counts_against_the_quota_and_writes_a_thought(self):
        sid, text = self.due(at(11))
        policy.share_said(self.s, sid, text)
        self.assertEqual(self.s.store.spoke_since(time.time() - 5), 1)
        self.assertIn(f'I told them: {text}', self.read('thoughts.md'))
        self.assertIsNone(self.s.store.waiting_share())          # said, no longer waiting

    def test_asked_question_becomes_asked_not_said(self):
        s = self.make(share_per_day=[2, 2], data_dir=self.dir + '/other')
        s.store.add_share(self.day, 'Do you cook for two?', kind='question')
        sid, text = s.share_due(last_talk_ts=at(11) - 5, now=at(11))
        s.share_said(sid, text)
        self.assertEqual(s.store.questions()[0]['state'], 'asked')
        self.assertIn('I asked them: Do you cook for two?', self.read('other', 'thoughts.md'))


class TestQuota(SoulCase):
    def test_quota_is_stable_for_a_date_and_within_range(self):
        cfg = self.make().cfg
        now = at(12)
        self.assertEqual(policy.quota(cfg, now), policy.quota(cfg, now + 60))
        seen = {policy.quota(cfg, at(12, days=d)) for d in range(60)}
        self.assertEqual(seen, {1, 2, 3})
        self.assertTrue(all(1 <= v <= 3 for v in seen))

    def test_a_zero_quota_never_speaks(self):
        s = self.make(share_per_day=[0, 0])
        s.store.add_share(time.strftime('%Y-%m-%d'), 'Anything.')
        self.assertIsNone(s.share_due(last_talk_ts=at(11) - 5, now=at(11)))


if __name__ == '__main__':
    unittest.main()
