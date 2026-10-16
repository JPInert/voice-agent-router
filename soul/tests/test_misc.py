import contextlib
import io
import os
import sys
import types
import unittest

from soul import Config
from soul.demo import load_fixture, materialise
from soul.text import json_from, speakable, words

from .helpers import FakeClient, SoulCase


class TestConfig(SoulCase):
    def toml(self, text):
        return self.write('soul.toml', text)

    def test_defaults_need_no_file(self):
        c = Config.load()
        self.assertEqual((c.user_name, c.wake_word, c.share_per_day), ('the user', 'computer', [1, 3]))

    def test_load_overrides_and_expands(self):
        c = Config.load(self.toml('user_name = "Sam"\nwake_word = "assistant"\nshare_hours = [10, 20]\n'
                                  'data_dir = "~/somewhere"\ncapabilities = ["lights", "music"]\n'))
        self.assertEqual((c.user_name, c.wake_word, c.share_hours, c.capabilities), ('Sam', 'assistant', [10, 20], ['lights', 'music']))
        self.assertEqual(c.dir, os.path.expanduser('~/somewhere'))
        self.assertTrue(c.models.endswith('somewhere/models/bge-small-en-v1.5'))

    def test_unknown_key_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'unknown config key.*user_nmae'):
            Config.load(self.toml('user_nmae = "typo"\n'))

    def test_validation(self):
        for bad in ('share_per_day = [3, 1]', 'share_hours = [21, 9]', 'share_hours = [9, 30]', 'idea_dup_sim = 1.5'):
            with self.assertRaises(ValueError, msg=bad):
                Config.load(self.toml(bad + '\n'))

    def test_env_config_and_db_override(self):
        p = self.toml('user_name = "Env"\n')
        os.environ['SOUL_CONFIG'] = p
        os.environ['SOUL_DB'] = '/tmp/x-override.db'
        try:
            c = Config.load()
            self.assertEqual((c.user_name, c.db_path), ('Env', '/tmp/x-override.db'))
        finally:
            del os.environ['SOUL_CONFIG'], os.environ['SOUL_DB']

    def test_example_file_loads(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        c = Config.load(os.path.join(here, 'soul.example.toml'))
        self.assertEqual(c.share_per_day, [1, 3])


class TestText(unittest.TestCase):
    def test_words(self):
        self.assertEqual(words('Remember that the spare key is in the blue bowl'), ['spare', 'key', 'blue', 'bowl'])

    def test_json_from_variants(self):
        self.assertEqual(json_from('{"a": 1}'), {'a': 1})
        self.assertEqual(json_from('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(json_from('Sure! {"a": {"b": 2}} hope that helps'), {'a': {'b': 2}})
        for bad in ('', None, '[1, 2]', 'no json', '{"a": '):
            self.assertIsNone(json_from(bad))

    def test_speakable(self):
        self.assertEqual(speakable('Say lights forty and it will happen.'), 'Say lights forty and it will happen.')
        self.assertEqual(speakable('Edit notes.md for that.'), 'Edit notes for that.')
        self.assertEqual(speakable('Try --dry-run first.'), 'Try dry run first.')
        self.assertNotIn('http', speakable('See https://example.com/x for details, ok?'))
        self.assertEqual(speakable('{"a": [1, 2]}'), '')
        self.assertEqual(speakable(''), '')


class TestLLM(unittest.TestCase):
    def test_anthropic_client_wraps_the_sdk(self):
        seen = {}

        class Msgs:
            def create(self, **kw):
                seen.update(kw)
                return types.SimpleNamespace(content=[types.SimpleNamespace(type='text', text='hello '),
                                                      types.SimpleNamespace(type='tool_use', text='ignored'),
                                                      types.SimpleNamespace(type='text', text='world')])

        class Anthropic:
            def __init__(self, **kw):
                seen['init'] = kw
                self.messages = Msgs()
        sys.modules['anthropic'] = types.SimpleNamespace(Anthropic=Anthropic)
        try:
            from soul.llm import AnthropicClient
            out = AnthropicClient(timeout_s=5).complete('SYS', 'USER', 'model-1', 77)
        finally:
            del sys.modules['anthropic']
        self.assertEqual(out, 'hello world')
        self.assertEqual((seen['model'], seen['max_tokens'], seen['system']), ('model-1', 77, 'SYS'))
        self.assertEqual(seen['messages'], [{'role': 'user', 'content': 'USER'}])


class TestCLI(SoulCase):
    def run_cli(self, *args):
        from soul.__main__ import main
        cfg = self.write('soul.toml', f'data_dir = "{self.dir}"\nuser_name = "Sam"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(['soul', '--config', cfg, *args])
        return rc, buf.getvalue()

    def test_approve_reject_with_a_bad_argument_print_usage_not_a_traceback(self):
        s = self.make()
        s.store.propose('Robin drinks oat milk in coffee', 0.9, 'e', '2026-03-01')
        for cmd in ('approve', 'reject'):
            rc, out = self.run_cli(cmd, 'foo')
            self.assertEqual(rc, 2)
            self.assertIn(f'usage: python3 -m soul {cmd} ID|all', out)
        self.assertEqual(len(s.pending()), 1)

    def test_roundtrip_of_remember_forget_pending_approve(self):
        self.assertEqual(self.run_cli('remember', 'the', 'spare', 'key', 'is', 'under', 'the', 'pot')[1].strip(), 'kept: the spare key is under the pot')
        self.assertIn('spare key', self.run_cli('memory')[1])
        s = self.make()
        s.store.propose('Robin drinks oat milk in coffee', 0.9, 'e', '2026-03-01')
        self.assertIn('Robin drinks oat milk in coffee', self.run_cli('pending')[1])
        self.assertIn('Robin drinks oat milk', self.run_cli('approve', 'all')[1])
        self.assertIn('nothing waiting', self.run_cli('pending')[1])
        self.assertIn('1 forgotten', self.run_cli('forget', 'spare key')[1])

    def test_db_search_and_usage(self):
        materialise(self.dir)
        rc, out = self.run_cli('db')
        self.assertEqual(rc, 0)
        self.assertIn('"heard"', out)
        self.assertIn('keyword-only', out)                                         # no model installed in the temp dir
        self.assertIn('Mochi', self.run_cli('search', 'Mochi', 'vet')[1])
        self.assertEqual(self.run_cli('nonsense')[0], 2)
        self.assertEqual(self.run_cli()[0], 2)


class TestDemoFixtures(SoulCase):
    def test_fixture_is_self_consistent(self):
        fx = load_fixture()
        self.assertTrue(all(c['expect'] in ('speak', 'silent') for c in fx['screen_cases']))
        self.assertGreaterEqual(sum(c['expect'] == 'silent' for c in fx['screen_cases']), 6)
        self.assertGreaterEqual(sum(c['expect'] == 'speak' for c in fx['screen_cases']), 3)

    def test_materialise_builds_a_searchable_household(self):
        materialise(self.dir)
        s = self.make(embedder=True)
        rows = s.search('Mochi vet appointment')
        self.assertTrue(any('vet' in r['text'] for r in rows))
        kinds = {k for k in s.store.stats() if isinstance(s.store.stats()[k], int)}
        self.assertTrue({'heard', 'said', 'routed', 'miss', 'thought', 'idea', 'memory', 'diary'} <= kinds)


if __name__ == '__main__':
    unittest.main()
