import asyncio

import pytest

from router.gate import AUTO, ASK, BLOCKED, PlanSlot, ToolPolicy, Verdict, no_attendees
from tests.helpers import stub_sdk


def test_unknown_tool_is_blocked():
    p = ToolPolicy(auto=['a'], blocked=['b'])
    assert (p.tier('a'), p.tier('b'), p.tier('never-heard-of-it')) == (AUTO, BLOCKED, BLOCKED)


def test_a_tool_in_two_tiers_is_an_error():
    with pytest.raises(ValueError):
        ToolPolicy(auto=['a'], blocked=['a'])


def test_ask_without_a_yes_channel_is_refused():
    p = ToolPolicy(ask=['wipe'])
    assert p.decide('wipe').tier == BLOCKED          # no asker: a missing channel is never a yes
    assert p.decide('wipe', ask=lambda prompt: False).tier == BLOCKED
    assert p.decide('wipe', ask=lambda prompt: True).tier == AUTO


def test_rule_can_refuse_or_rewrite():
    p = ToolPolicy(auto=['cal'], rules={'cal': no_attendees})
    v = p.decide('cal', {'title': 'x', 'attendees': ['Sam']})
    assert v.tier == BLOCKED and 'attendees' in v.message
    v = p.decide('cal', {'title': 'x'})
    assert v.tier == AUTO and v.updated_input == {'title': 'x', 'notificationLevel': 'NONE'}


def test_rule_cannot_unblock_a_blocked_tool():
    p = ToolPolicy(blocked=['cal'], rules={'cal': lambda inp: Verdict(AUTO)})
    assert p.decide('cal').tier == BLOCKED


def test_sdk_layers():
    """allowed_tools pre-approves, disallowed_tools is the hard deny, rule-bound tools stay out of allowed."""
    p = ToolPolicy(auto=['read', 'cal'], blocked=['send'], rules={'cal': no_attendees})
    kw = p.sdk_kwargs(stub_sdk())
    assert kw['allowed_tools'] == ['read']
    assert kw['disallowed_tools'] == ['send']
    cb = kw['can_use_tool']
    run = lambda *a: asyncio.run(cb(*a))
    assert run('cal', {'title': 't'}, None).updated_input['notificationLevel'] == 'NONE'
    assert run('cal', {'attendees': ['x']}, None).message.startswith('No attendees')
    assert run('something-new', {}, None).message == 'Not allowed from the voice assistant.'


def test_decisions_are_remembered():
    p = ToolPolicy(auto=['a'])
    p.decide('a'), p.decide('b')
    assert list(p.decisions) == [('a', AUTO), ('b', BLOCKED)]


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_plan_needs_two_yeses_and_runs_once():
    c = Clock()
    s = PlanSlot(clock=c)
    assert s.approve() == 'There is no plan waiting for approval.'
    s.propose('clean up the downloads folder.')
    assert s.state() == {'open': True, 'armed': False}
    assert s.confirm()[0] is None                     # "confirm" before "go ahead" does nothing
    assert 'clean up the downloads folder.' in s.approve()
    c.t += 30
    assert s.confirm() == ('clean up the downloads folder.', None)
    assert s.confirm()[0] is None                     # single use


def test_confirm_window_and_plan_expiry():
    c = Clock()
    s = PlanSlot(clock=c)
    s.propose('do it.')
    s.approve()
    c.t += 61
    assert 'Nothing is waiting for a confirm' in s.confirm()[1]
    s.approve()                                       # can be re-armed while the plan lives
    c.t += 540
    assert s.confirm()[0] is None and 'expired' in s.confirm()[1]


def test_intercept_only_acts_when_there_is_something_to_act_on():
    c = Clock()
    s = PlanSlot(clock=c)
    assert s.intercept('yes please') is None          # no plan: just conversation
    assert s.intercept('computer, confirm') is None
    s.propose('x.')
    assert s.intercept('go ahead') == 'approve'
    assert s.intercept('computer confirm') is None    # not armed yet
    s.approve()
    assert s.intercept('computer, confirm') == 'confirm'
    assert s.intercept('what is the weather') is None
