import asyncio

from router.gate import AUTO, BLOCKED
from router.registry import ToolRegistry
from tests.helpers import stub_sdk


def make():
    calls = []
    reg = ToolRegistry('house')
    reg.add('read', 'reads', {'q': str}, lambda q='': calls.append(('read', q)) or 'value')
    reg.add('write', 'writes', {'x': str}, lambda x='': calls.append(('write', x)) or 'done', writes=True)
    reg.add('mixed', 'nav writes, battery reads', {'action': str},
            lambda action: calls.append(('mixed', action)) or 'ok', writes=lambda a: a.get('action') == 'nav')
    reg.add('boom', 'fails', {}, lambda: (_ for _ in ()).throw(RuntimeError('secret-token-abc')))
    return reg, calls


def test_dry_never_reaches_a_write():
    reg, calls = make()
    assert reg.run('write', {'x': 'a'}, dry=True).startswith('DRY RUN: would call write(')
    assert reg.run('mixed', {'action': 'nav'}, dry=True).startswith('DRY RUN')
    assert calls == []
    assert reg.run('read', {'q': 'z'}, dry=True) == 'value'          # reads still run in a dry run
    assert reg.run('mixed', {'action': 'battery'}, dry=True) == 'ok'
    assert calls == [('read', 'z'), ('mixed', 'battery')]


def test_failure_reports_the_class_not_the_message():
    reg, _ = make()
    out = reg.run('boom', {})
    assert out == 'boom failed (RuntimeError).' and 'secret' not in out


def test_unknown_tool_and_bad_arguments():
    reg, calls = make()
    assert reg.run('nope', {}) == 'Unknown tool nope.'
    assert 'arguments it does not take' in reg.run('read', {'surprise': 1})
    assert calls == []


def test_duplicate_name_and_bad_tier():
    reg, _ = make()
    for bad in (lambda: reg.add('read', 'x', {}, lambda: ''), lambda: reg.add('t', 'x', {}, lambda: '', tier='maybe')):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError('expected ValueError')


def test_policy_from_tiers_and_extras():
    reg, _ = make()
    reg.add('danger', 'never', {}, lambda: '', tier=BLOCKED)
    p = reg.policy(extra_auto=['WebSearch'], extra_blocked=['mcp__mail__send'])
    assert p.tier('mcp__house__read') == AUTO
    assert p.tier('mcp__house__danger') == BLOCKED
    assert p.tier('WebSearch') == AUTO and p.tier('mcp__mail__send') == BLOCKED
    assert p.tier('mcp__house__unregistered') == BLOCKED


def test_sdk_server_wraps_every_tool_and_honours_dry():
    reg, calls = make()
    server = reg.sdk_server(stub_sdk(), dry=True)
    assert server.name == 'house'
    assert sorted(t.tool_name for t in server.tools) == ['boom', 'mixed', 'read', 'write']
    write = next(t for t in server.tools if t.tool_name == 'write')
    out = asyncio.run(write({'x': 'q'}))
    assert out['content'][0]['text'].startswith('DRY RUN') and calls == []


def test_rows_describe_each_tool():
    reg, _ = make()
    rows = {r['tool']: r for r in reg.rows()}
    assert rows['write']['writes'] is True and rows['read']['writes'] is False and rows['mixed']['writes'] == 'some'
