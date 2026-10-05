import os

from router import tricks

EXAMPLE = os.path.join(os.path.dirname(__file__), '..', 'examples', 'tricks.yaml')


def test_example_file_loads():
    ts = tricks.load(EXAMPLE)
    assert len(ts) == 7


def test_whole_utterance_match_ignores_case_and_punctuation():
    ts = tricks.load(EXAMPLE)
    assert tricks.match('Movie night.', ts).name == 'movie night'
    assert tricks.match('movie night with popcorn', ts) is None    # phrases are whole-utterance


def test_regex_match():
    ts = tricks.load(EXAMPLE)
    assert tricks.match('Turn on the fan.', ts).name == 'fan on'


def test_shell_forces_confirm_unless_readonly():
    t = tricks.build({'name': 'x', 'phrases': ['x'], 'actions': [{'shell': ['touch', 'f']}]})
    assert t.confirm
    t = tricks.build({'name': 'y', 'phrases': ['y'], 'readonly': True,
                      'actions': [{'shell': ['df', '-h']}]})
    assert not t.confirm


def test_unconfirmed_trick_does_not_run():
    ran = []
    t = tricks.build({'name': 'x', 'phrases': ['x'], 'actions': [{'say': 'hi'}], 'confirm': True})
    ok, note = tricks.run(t, tricks.TrickCtx(say=ran.append, confirm=lambda p: False))
    assert (ok, ran) == (False, [])


def test_bad_entry_is_skipped_not_fatal(tmp_path):
    p = tmp_path / 't.yaml'
    p.write_text('- name: good\n  phrases: [a]\n  actions: [{say: hi}]\n'
                 '- name: bad\n  phrases: [b]\n  actions: [{teleport: now}]\n')
    assert [t.name for t in tricks.load(str(p))] == ['good']
