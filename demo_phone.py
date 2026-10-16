#!/usr/bin/env python3
"""The phone-miss worker against fake devices. Nothing real is touched.

    python3 demo_phone.py                       # offline: a scripted "model", the real gate and tools
    python3 demo_phone.py --live "set a timer for ten minutes"
                                                # a real Sonnet session (needs `claude` logged in),
                                                # still against the fakes; prints what the fakes recorded

Offline, five synthetic requests are replayed with the tool calls a model would plausibly make,
including two it must not be allowed to complete (a send, an invite). The tools and the permission
gate are the real ones; only the model is replaced. With --live the hosted mail/calendar connectors are
not classified in the policy, so any of their tools a logged-in session offers is refused (deny by default).
"""
import argparse
import json

from router import fakes
from router.connectors.hosted import CALENDAR, GMAIL
from router.phone_worker import PhoneWorker, build_registry, default_policy

H = 'mcp__house__'
SCRIPTS = [
    ('add dentist tomorrow at 3 PM', [
        ('tool', H + 'calendar_add', {'title': 'Dentist', 'start': '2026-10-17T15:00'}),
        ('say', 'Added Dentist tomorrow at 3 PM.')]),
    ('send the landlord an email that rent is coming', [
        ('tool', GMAIL + 'send_message', {'to': 'landlord', 'body': 'Rent is coming.'}),
        ('tool', H + 'mail_draft', {'to': 'landlord', 'subject': 'Rent', 'body': 'Rent is coming.'}),
        ('say', 'I cannot send email, but I saved a draft for you.')]),
    ('invite Sam to dinner friday', [
        ('tool', CALENDAR + 'create_event', {'title': 'Dinner', 'start': '2026-10-23T19:00',
                                             'attendees': ['Sam']}),
        ('say', 'I cannot invite people, only add to your own calendar.')]),
    ('send the car to the museum', [
        ('tool', H + 'car', {'action': 'nav', 'place': 'museum'}),
        ('say', 'Sent the museum to the car.')]),
    ('set a timer for ten minutes', [
        ('tool', H + 'phone_do', {'action': 'timer', 'value': '600', 'label': 'timer'}),
        ('say', 'Ten minute timer is running on your phone.')]),
]


def make_fakes():
    return dict(car=fakes.FakeCar(), phone=fakes.FakePhone(), mail=fakes.FakeMail(),
                calendar=fakes.FakeCalendar(), home=fakes.FakeHome(), food=fakes.FakeFood())


def make_registry(f):
    return build_registry(home_backend=f['home'], reflex=lambda p: (False, []), car_backend=f['car'],
                          mail_backend=f['mail'], calendar_backend=f['calendar'], phone_backend=f['phone'],
                          food_backend=f['food'])


def offline():
    for text, steps in SCRIPTS:
        f = make_fakes()
        reg = make_registry(f)
        runner = fakes.ScriptedRunner(steps)
        out = PhoneWorker(reg, default_policy(reg, hosted_connectors=True), runner=runner).run(text)
        print(f'> {text}')
        for full, args, tier, result in runner.transcript:
            print(f'    {full.replace("mcp__", "")}  [{tier}]  {result}')
        print(f'  say: {out["sentence"]}')
        for k, v in (('drafts', f['mail'].drafts), ('calendar', f['calendar'].added),
                     ('car', f['car'].calls), ('phone', f['phone'].calls)):
            if v:
                print(f'  fake {k}: {json.dumps(v)}')


def live(text):
    f = make_fakes()
    reg = make_registry(f)
    out = PhoneWorker(reg, default_policy(reg)).run(text)
    print(json.dumps(out, indent=1))
    for k in ('car', 'phone'):
        print(f'fake {k} recorded: {f[k].calls}')
    print(f'fake calendar added: {f["calendar"].added}; drafts: {f["mail"].drafts}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', metavar='TEXT', help='run a real Sonnet session on TEXT against the fakes')
    a = ap.parse_args()
    live(a.live) if a.live else offline()
