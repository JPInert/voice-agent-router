import json
import subprocess

import pytest

from router import fakes, tricks
from router.connectors import calendar, car, food, home, kodi, mail, phone, wish
from router.registry import ToolRegistry

STATES = [
    {'entity_id': 'lock.front_door', 'state': 'locked', 'attributes': {'friendly_name': 'Front door lock'}},
    {'entity_id': 'sensor.living_room_temperature', 'state': '71',
     'attributes': {'friendly_name': 'Living room temperature', 'unit_of_measurement': 'F'}},
    {'entity_id': 'light.living_room', 'state': 'on', 'attributes': {'friendly_name': 'Living room light'}},
]


def test_house_status_default_and_query():
    h = fakes.FakeHome(STATES)
    default = home.house_status(h)
    assert 'Front door lock: locked' in default and 'Living room temperature: 71 F' in default
    assert 'light' not in default.lower()                     # default view is locks, doors, temperatures
    assert home.house_status(h, 'living room light') .startswith('Living room light: on')
    assert 'No Home Assistant entity matches' in home.house_status(h, 'garage')


TRICKS = """
- name: movie night
  phrases: ["movie night"]
  actions:
    - ha: {service: light.turn_off, entity_id: light.living_room}
    - say: "Enjoy the movie."
- name: wipe
  phrases: ["wipe the cache"]
  actions:
    - shell: ["true"]
"""


def reflex(tmp_path):
    p = tmp_path / 'tricks.yaml'
    p.write_text(TRICKS)
    ha = fakes.FakeHome()
    return home.Reflex(tricks.Loader(str(p)), ha=ha), ha


def test_house_runs_only_defined_phrases(tmp_path):
    r, ha = reflex(tmp_path)
    assert home.house_command(r, 'Movie night.') == 'Enjoy the movie.'
    assert ha.calls == [('light', 'turn_off', {'entity_id': 'light.living_room'})]
    assert 'did not recognise' in home.house_command(r, 'unlock the front door')
    assert len(ha.calls) == 1                                  # no phrase, no call: the model cannot invent one


def test_a_trick_that_needs_a_spoken_yes_is_refused_from_the_phone(tmp_path):
    r, _ = reflex(tmp_path)
    assert home.house_command(r, 'wipe the cache') == "I can't confirm that from the phone."


def test_now_playing():
    assert kodi.now_playing(fakes.FakeKodi()) == 'The TV (Kodi) is not playing anything.'
    k = fakes.FakeKodi(players=[{'playerid': 1}], item={'showtitle': 'Show', 'season': 2, 'episode': 3, 'title': 'Ep'},
                       props={'percentage': 41.6, 'speed': 0})
    assert kodi.now_playing(k) == 'Kodi: Show season 2 episode 3, Ep, 42% in, paused.'


def test_car_actions_are_an_allow_list():
    c = fakes.FakeCar()
    for bad in ('unlock', 'lock', 'trunk', 'windows', 'honk', 'flash', 'sentry', 'drive', ''):
        assert car.car(c, bad, 'x').startswith('Car actions:')
    assert c.calls == []                                        # nothing reached the backend


def test_car_nav_stop_reads_and_missing_place():
    c = fakes.FakeCar()
    assert car.car(c, 'nav', 'the museum') == 'Sent the museum to the car.'
    assert car.car(c, 'stop', 'coffee') == 'Adding coffee as a stop now; the car will say when it is in.'
    assert car.car(c, 'battery') == 'The car is at 64 percent.'
    assert car.car(c, 'nav') == 'Which place?' and c.calls[-1] == ('battery', '')


def test_car_nav_timeout_is_not_a_failure():
    c = fakes.FakeCar('asleep')
    assert 'still waking up' in car.car(c, 'nav', 'x')
    c._state = 'online'            # same timeout, but the service reports online: it is only slow
    c.act = lambda action, place='': (_ for _ in ()).throw(subprocess.TimeoutExpired('car', 20))
    assert 'Still sending it to the car' in car.car(c, 'nav', 'x')


def test_car_history():
    assert car.car(fakes.FakeCar(), 'history') == 'No destination history yet.'
    out = car.car(fakes.FakeCar(history=[{'label': 'Museum', 'q': 'the museum'}]), 'history')
    assert 'Museum (asked as "the museum")' in out and 'not proof' in out


def test_cli_car_backend_parses_the_last_json_line(monkeypatch):
    def run(argv, **kw):
        assert argv == ['car.py', 'nav', 'museum']
        return subprocess.CompletedProcess(argv, 0, stdout='noise\n{"sentence": "Sent."}\n', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    assert car.CliCarBackend(['car.py']).act('nav', 'museum') == {'sentence': 'Sent.', 'ok': True}


def test_mail_cannot_send():
    m = fakes.FakeMail([{'id': '1', 'from': 'sender-a', 'subject': 'Rent', 'body': 'Pay by Friday.'}])
    for backend in (m, ):
        assert not any(hasattr(backend, n) for n in ('send', 'send_message', 'reply', 'forward'))
    assert mail.search(m, 'rent') == '1: sender-a: Rent'
    assert 'Pay by Friday.' in mail.get(m, '1') and mail.get(m, '9') == 'No such message.'
    assert 'nothing was sent' in mail.draft(m, 'sender-a', 'Re: Rent', 'Will do.') and len(m.drafts) == 1


def test_calendar_add_refuses_attendees_twice():
    c = fakes.FakeCalendar([{'start': '2026-10-17T09:00', 'title': 'Standup'}])
    assert calendar.list_events(c) == '2026-10-17T09:00: Standup'
    assert calendar.add_event(c, 'Dentist', '2026-10-17T15:00').startswith('Added')
    assert calendar.add_event(c, 'Dinner', '2026-10-23T19:00', attendees=['Sam']).startswith('Refused')
    assert [e['title'] for e in c.added] == ['Dentist']


def test_phone_validation():
    assert phone.validate('timer', '600')[1] == '600'
    for bad in (('timer', '0'), ('timer', '90000'), ('timer', '5; reboot'), ('alarm', '25:00'), ('alarm', '7'),
                ('app', '!!'), ('reboot', '')):
        assert phone.validate(*bad)[0], bad
    assert phone.validate('alarm', '7:05')[1] == '7:05'


def test_phone_url_must_be_plain_http(tmp_path):
    p = fakes.FakePhone()
    for bad in ('javascript:alert(1)', 'file:///etc/passwd', 'https://x.example/a b', "https://x.example/'; reboot",
                'https://x.example/`id`', ''):
        assert phone.open_url(p, bad) == 'Refused: not a plain web URL.'
    assert p.calls == []
    assert phone.open_url(p, 'https://example.com/a?b=1') == 'Opened on the phone.'


def test_adb_phone_builds_quoted_intents():
    seen = []

    def run(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
    p = phone.AdbPhone(adb='adb', serial='S1', run=run)
    p.open_url('https://example.com/a?b=1&c=2')
    p.timer(600, "it's tea")
    assert seen[0][:4] == ['adb', '-s', 'S1', 'shell']
    assert "-d 'https://example.com/a?b=1&c=2'" in seen[0][4]        # & is quoted, not a shell operator
    assert "'it'\"'\"'s tea'" in seen[1][4] and 'LENGTH 600' in seen[1][4]


def test_food_log_amount_guard():
    f = fakes.FakeFood()
    assert 'outside 0 to 10' in food.food(f, ['log', '--food-id', 'x', '--units', '20'])
    assert food.food(f, ['log', '--food-id', 'x']).startswith('Refused: log needs --units')
    assert food.food(f, ['create', 'x']).startswith('Food commands:')       # no create, no weight
    assert f.calls == []
    assert 'ok' in food.food(f, ['log', '--food-id', 'x', '--units', '1.5'])
    assert 'ok' in food.food(f, ['day']) and len(f.calls) == 2


def test_food_dry_run_guard_covers_list_and_string_args():
    from router.registry import ToolRegistry
    f = fakes.FakeFood()
    reg = ToolRegistry('house')
    food.register(reg, f)
    for args in (['log', '--food-id', 'x', '--units', '1'], ['delete', '7'],
                 'log --food-id x --units 1', 'delete 7', 'day'):          # strings must not slip past
        assert reg.run('food', {'args': args}, dry=True).startswith('DRY RUN'), args
    assert f.calls == []
    assert reg.run('food', {'args': ['day']}, dry=True).startswith('{')       # a clean list read still runs
    assert reg.run('food', {'args': 'log --food-id x --units 1'}).startswith('Refused: args must be a list')
    assert len(f.calls) == 1


def test_wish_appends_a_line(tmp_path):
    path = tmp_path / 'sub' / 'wishlist.jsonl'
    assert wish.wish(str(path), 'water the plants') == 'Added to the wishlist.'
    assert json.loads(path.read_text())['what'] == 'water the plants'
    assert wish.wish(str(path), '  ') == 'Nothing to note.'


def test_every_connector_registers_into_one_registry():
    from router.phone_worker import build_registry
    reg = build_registry(home_backend=fakes.FakeHome(), reflex=lambda p: (False, []), kodi_backend=fakes.FakeKodi(),
                         car_backend=fakes.FakeCar(), mail_backend=fakes.FakeMail(),
                         calendar_backend=fakes.FakeCalendar(), phone_backend=fakes.FakePhone(),
                         food_backend=fakes.FakeFood(), wishlist_path='/tmp/unused-wishlist.jsonl')
    assert sorted(reg.tools) == ['calendar_add', 'calendar_list', 'car', 'food', 'house', 'house_status', 'mail_draft',
                                 'mail_get', 'mail_search', 'now_playing', 'phone_do', 'phone_open', 'wish']
