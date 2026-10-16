"""docs/CAPABILITIES.md must name each tool the extract registers, and every hosted tool it classifies."""
import os

from router import fakes
from router.connectors import hosted
from router.phone_worker import build_registry

DOC = open(os.path.join(os.path.dirname(__file__), '..', 'docs', 'CAPABILITIES.md')).read()


def test_every_registered_tool_is_in_the_doc():
    reg = build_registry(home_backend=fakes.FakeHome(), reflex=lambda p: (False, []), kodi_backend=fakes.FakeKodi(),
                         car_backend=fakes.FakeCar(), mail_backend=fakes.FakeMail(),
                         calendar_backend=fakes.FakeCalendar(), phone_backend=fakes.FakePhone(),
                         food_backend=fakes.FakeFood(), wishlist_path='/tmp/unused-wishlist.jsonl')
    missing = [n for n in reg.tools if f'`{n}`' not in DOC]
    assert not missing, f'tools missing from docs/CAPABILITIES.md: {missing}'


def test_every_blocked_connector_tool_is_named_in_the_doc():
    names = {n.rsplit('__', 1)[-1] for n in hosted.BLOCKED}
    missing = sorted(n for n in names if n not in DOC)
    assert not missing, f'blocked connector tools missing from docs/CAPABILITIES.md: {missing}'
