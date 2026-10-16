"""Calendar: read events, and ADD one to the owner's own calendar.

Adding is allowed because it is undoable and private, on three conditions kept in code:
no attendees (an invite is a message to someone else), no notifications, and no update, delete or
RSVP tool at all. The first two are `gate.no_attendees`, applied as the tool's rule so it also
holds when the model supplies `attendees` in the arguments; the backend refuses them again.
"""
from ..gate import no_attendees


def list_events(backend, start='', end=''):
    ev = backend.list_events(str(start), str(end))
    return '\n'.join(f"{e['start']}: {e['title']}" for e in ev[:25]) or 'Nothing on the calendar in that window.'


def add_event(backend, title, start, end='', description='', attendees=None):
    if attendees:
        return 'Refused: no attendees, the owner\'s own calendar only.'
    backend.add_event(str(title)[:200], str(start), str(end), str(description)[:1000])
    return f'Added "{title}" at {start}.'


def register(reg, backend):
    reg.add('calendar_list', 'List events between start and end (ISO dates).', {'start': str, 'end': str},
            lambda start='', end='': list_events(backend, start, end), source='calendar')
    reg.add('calendar_add', 'Add an event to the owner\'s OWN calendar (never with attendees).',
            {'title': str, 'start': str, 'end': str, 'description': str, 'attendees': list},
            lambda title, start, end='', description='', attendees=None, notificationLevel=None:
            add_event(backend, title, start, end, description, attendees),
            writes=True, rule=no_attendees, source='calendar')
