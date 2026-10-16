import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request

from router import fakes
from router.connectors.hosted import CALENDAR, CALENDAR_ADD, GMAIL
from router.gate import AUTO, BLOCKED
from router.phone_jobs import PhoneJobs, handle_phone
from router.phone_worker import (PhoneWorker, SdkRunner, build_registry, default_policy, speakable_reply,
                                 system_prompt)
from tests.helpers import stub_sdk

H = 'mcp__house__'


def world():
    f = dict(car=fakes.FakeCar(), phone=fakes.FakePhone(), mail=fakes.FakeMail(), calendar=fakes.FakeCalendar(),
             home=fakes.FakeHome(), food=fakes.FakeFood())
    reg = build_registry(home_backend=f['home'], reflex=lambda p: (False, []), car_backend=f['car'],
                         mail_backend=f['mail'], calendar_backend=f['calendar'], phone_backend=f['phone'],
                         food_backend=f['food'])
    return f, reg


def run(steps, reg, **kw):
    runner = fakes.ScriptedRunner(steps)
    out = PhoneWorker(reg, default_policy(reg, hosted_connectors=True), runner=runner, **kw).run('synthetic')
    return out, runner


def test_own_tools_auto_approve_and_act():
    f, reg = world()
    out, r = run([('tool', H + 'car', {'action': 'nav', 'place': 'museum'}),
                  ('tool', H + 'phone_do', {'action': 'timer', 'value': '60'}),
                  ('say', 'Done.')], reg)
    assert [t[2] for t in r.transcript] == [AUTO, AUTO]
    assert f['car'].calls == [('nav', 'museum')] and f['phone'].calls[0][:2] == ('timer', '60')
    assert out['sentence'] == 'Done.' and out['tools'] == ['car', 'phone_do']


def test_outbound_to_others_is_blocked_and_never_reaches_a_backend():
    f, reg = world()
    steps = [('tool', GMAIL + 'send_message', {'to': 'landlord'}),
             ('tool', GMAIL + 'reply', {}), ('tool', GMAIL + 'forward', {}),
             ('tool', CALENDAR + 'respond_to_event', {}), ('tool', CALENDAR + 'delete_event', {}),
             ('tool', CALENDAR_ADD, {'title': 'x', 'attendees': ['landlord']}),
             ('say', 'I cannot.')]
    out, r = run(steps, reg)
    assert [t[2] for t in r.transcript] == [BLOCKED] * 6
    assert f['mail'].drafts == [] and f['calendar'].added == []


def test_unknown_and_unregistered_tools_are_refused():
    f, reg = world()
    _, r = run([('tool', H + 'unlock_front_door', {}), ('tool', 'Bash', {'command': 'ls'}), ('say', 'x')], reg)
    assert [t[2] for t in r.transcript] == [BLOCKED, BLOCKED]


def test_worker_without_a_backend_has_no_such_tool():
    reg = build_registry(car_backend=fakes.FakeCar())
    assert list(reg.tools) == ['car']
    assert 'mail_draft' not in system_prompt(reg)


def test_dry_run_changes_nothing():
    f, reg = world()
    out = PhoneWorker(reg, runner=fakes.ScriptedRunner([('tool', H + 'car', {'action': 'nav', 'place': 'x'}),
                                                        ('say', 'ok')])).run('x', dry=True)
    assert f['car'].calls == [] and out['dry'] is True


def test_the_worker_always_returns_a_sentence():
    f, reg = world()

    class Slow:
        async def run(self, *a, **k):
            await asyncio.sleep(5)

    class Broken:
        async def run(self, *a, **k):
            raise ConnectionError('the-request-text-must-not-leak')

    class Silent:
        async def run(self, *a, **k):
            return '', []
    assert PhoneWorker(reg, runner=Slow(), deadline_s=0.05).run('x')['sentence'].startswith('That took too long')
    broken = PhoneWorker(reg, runner=Broken()).run('x')
    assert broken['tools'] == ['error:ConnectionError'] and 'leak' not in json.dumps(broken)
    assert PhoneWorker(reg, runner=Silent()).run('x')['sentence'] == 'I tried, but came up empty.'


def test_speakable_reply():
    t = 'Added **Dentist** at [3 PM](https://example.com/x) \u2014 see https://example.com.\n\nSources:\n- a'
    assert speakable_reply(t) == 'Added Dentist at 3 PM, see'


def test_system_prompt_lists_the_registered_tools_and_the_limits():
    f, reg = world()
    p = system_prompt(reg, away=True, now='Friday October 16 2026, 9:00 AM')
    assert 'AWAY from home' in p and '- car:' in p and '- mail_draft:' in p
    assert 'cannot send email' in p and 'never fill in numbers' in p


def test_sdk_options_wire_all_three_layers():
    f, reg = world()
    pol = default_policy(reg, hosted_connectors=True)
    opts = SdkRunner().options(stub_sdk(), 'prompt', reg, pol, model='m').kw
    assert opts['model'] == 'm' and opts['setting_sources'] == []           # the owner's settings never load
    assert opts['tools'] == ['WebSearch', 'WebFetch']                        # no Bash, no Edit, no Write
    assert GMAIL + 'send_message' in opts['disallowed_tools']
    assert CALENDAR + 'respond_to_event' in opts['disallowed_tools']
    assert H + 'car' in opts['allowed_tools'] and 'WebSearch' in opts['allowed_tools']
    from router.connectors import hosted
    assert set(hosted.BLOCKED) <= set(opts['disallowed_tools'])               # incl. Drive and Spotify writes
    assert CALENDAR_ADD not in opts['allowed_tools']                         # rule-bound: must reach the callback
    assert set(opts['mcp_servers']) == {'house'} and callable(opts['can_use_tool'])


def test_default_model_is_overridable(monkeypatch):
    import importlib
    import router.phone_worker as pw
    monkeypatch.setenv('ROUTER_WORKER_MODEL', 'some-other-model')
    assert importlib.reload(pw).MODEL == 'some-other-model'
    monkeypatch.delenv('ROUTER_WORKER_MODEL')
    assert importlib.reload(pw).MODEL == 'claude-sonnet-4-5'


# --- the phone side ------------------------------------------------------------------------------

def make_jobs(steps=(('say', 'Done.'),), log=None):
    f, reg = world()
    return PhoneJobs(PhoneWorker(reg, runner=fakes.ScriptedRunner(steps)), log=log or (lambda *a: None))


def test_a_miss_from_the_owner_starts_the_worker():
    jobs = make_jobs()
    r = handle_phone('what is the capital of france', 'owner', lambda t: (False, []), jobs)
    assert r['what'] == 'claude' and r['said'] == ['On it.']
    assert jobs.collect(r['pending'], wait_s=5)['sentence'] == 'Done.'


def test_tier_zero_answers_first_and_no_worker_starts():
    jobs = make_jobs()
    r = handle_phone('movie night', 'owner', lambda t: (True, ['Enjoy.']), jobs)
    assert r == {'miss': False, 'what': 'trick', 'text': 'movie night', 'said': ['Enjoy.']} and not jobs.jobs


def test_a_guest_phone_never_reaches_the_worker():
    jobs = make_jobs()
    r = handle_phone('read my mail', 'guest', lambda t: (False, []), jobs)
    assert r['miss'] is True and not jobs.jobs


def test_the_workers_own_house_calls_cannot_start_another_worker():
    jobs = make_jobs()
    r = handle_phone('lights', 'owner', lambda t: (False, []), jobs, no_claude=True)
    assert r['miss'] is True and not jobs.jobs


def test_empty_text_is_a_miss():
    assert handle_phone('  ', 'owner', lambda t: (False, []), make_jobs())['why'] == 'nothing heard'


def test_collect_unknown_id_and_still_working():
    jobs = make_jobs()
    assert jobs.collect('nope') is None
    slow = threading.Event()

    class Slow:
        async def run(self, *a, **k):
            await asyncio.get_running_loop().run_in_executor(None, slow.wait, 5)
            return 'late', []
    f, reg = world()
    j = PhoneJobs(PhoneWorker(reg, runner=Slow()), log=lambda *a: None)
    jid = j.start('x')
    assert j.collect(jid, wait_s=0.05)['pending'] is True
    slow.set()
    assert j.collect(jid, wait_s=5)['sentence'] == 'late'


def test_old_jobs_are_purged():
    now = [1000.0]
    f, reg = world()
    j = PhoneJobs(PhoneWorker(reg, runner=fakes.ScriptedRunner([('say', 'x')])), ttl_s=600, clock=lambda: now[0],
                  log=lambda *a: None)
    first = j.start('a')
    j.collect(first, 5)
    now[0] += 601
    j.start('b')
    assert first not in j.jobs


def test_job_log_lines_carry_no_words_and_feed_the_measure_tool(tmp_path):
    lines = []
    jobs = make_jobs([('tool', H + 'car', {'action': 'battery'}), ('say', 'ok')], log=lines.append)
    jid = jobs.start('my private request words')
    jobs.collect(jid, 5)
    assert not any('private' in l for l in lines)
    assert lines[-1].startswith(f'phone claude {jid}: rc=0 ') and "tools=['car']" in lines[-1]
    log = tmp_path / 'v.log'
    log.write_text(''.join(f'10-12 09:00:0{i} {l}\n' for i, l in enumerate(lines)))
    from tools.measure_phone_jobs import parse
    start, done = parse(str(log))
    assert list(start) == [jid] and done[jid]['tools'] == ['car']


def test_measure_tool_on_the_synthetic_fixture(capsys, monkeypatch):
    import sys
    from tools import measure_phone_jobs
    fx = os.path.join(os.path.dirname(__file__), 'fixtures', 'phone_jobs.log')
    monkeypatch.setattr(sys, 'argv', ['m', fx, '--year', '2026'])
    measure_phone_jobs.main()
    out = capsys.readouterr().out
    assert 'window   2026-10-02 09:00:00 -> 2026-10-04 11:00:00' in out
    assert 'started  5   finished 5' in out
    assert 'regex intercept (no model): 1' in out
    assert 'model sessions: 4  (with a tool call 3, none 1)' in out
    assert "tools: {'house': 2, 'car': 1, 'WebSearch': 1}" in out
    assert 'seconds  n=4  median=8.0  p90=20.0' in out


def test_http_endpoint_needs_the_token_and_serves_the_two_calls():
    import phone_server
    from http.server import ThreadingHTTPServer
    f, reg = world()
    phone_server.H.reflex = staticmethod(lambda t: (t == 'movie night', ['Enjoy.']))
    phone_server.H.jobs = PhoneJobs(PhoneWorker(reg, runner=fakes.ScriptedRunner([('say', 'Hello.')])),
                                    log=lambda *a: None)
    phone_server.H.bearer = 'for-tests-only'
    srv = ThreadingHTTPServer(('127.0.0.1', 0), phone_server.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_address[1]}'

    def post(path, body, auth='for-tests-only'):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': f'Bearer {auth}'} if auth else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert post('/phone', {'text': 'x'}, auth=None)[0] == 401
        assert post('/phone', {'text': 'x'}, auth='wrong')[0] == 401
        assert post('/phone', {'text': 'movie night'})[1]['what'] == 'trick'
        st, body = post('/phone', {'text': 'what time is it', 'who': 'owner'})
        assert body['what'] == 'claude'
        assert post('/phone/pending', {'id': body['pending']})[1]['sentence'] == 'Hello.'
        assert post('/phone/pending', {'id': 'nope'})[0] == 404
        assert post('/phone', {'text': 'what time is it', 'who': 'guest'})[1]['miss'] is True
        assert post('/phone', {'text': 'what time is it'})[1]['miss'] is True      # no `who`: a guest, not the owner
        assert post('/phone', {'text': 'what time is it', 'who': 'owner'})[1]['what'] == 'claude'
    finally:
        srv.shutdown()
