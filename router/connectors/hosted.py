"""Hosted MCP connectors (mail, calendar, drive, music): tool names and their tiers.

When an account is attached through a hosted connector rather than an adapter in this package, its
tools arrive in the session by name. Nothing to implement: only to classify, which is what the
production worker's two lists did. Reads and in-the-house writes are AUTO; anything that sends,
shares, deletes or answers on the owner's behalf is BLOCKED. The names below are the connector's
own tool names (claude.ai Gmail / Google Calendar / Google Drive / Spotify connectors), checked
against the production allow and deny lists.
"""
from ..gate import no_attendees

GMAIL, CALENDAR, DRIVE, SPOTIFY = ('mcp__claude_ai_Gmail__', 'mcp__claude_ai_Google_Calendar__',
                                   'mcp__claude_ai_Google_Drive__', 'mcp__claude_ai_Spotify__')

AUTO = ([GMAIL + n for n in ('search_threads', 'get_thread', 'get_message', 'list_labels', 'list_drafts',
                             'get_draft', 'create_draft')] +
        [CALENDAR + n for n in ('list_events', 'get_event', 'search_events', 'list_calendars', 'suggest_time')] +
        [DRIVE + n for n in ('search_files', 'read_file_content', 'get_file_metadata', 'list_recent_files')] +
        [SPOTIFY + n for n in ('get_currently_playing', 'search')] +
        ['WebSearch', 'WebFetch'])

# create_event is AUTO only through a rule: no attendees, no notifications (gate.no_attendees).
# ToolPolicy keeps rule-bound tools out of allowed_tools so the rule always runs.
CALENDAR_ADD = CALENDAR + 'create_event'
AUTO.append(CALENDAR_ADD)
RULES = {CALENDAR_ADD: no_attendees}

BLOCKED = ([GMAIL + n for n in ('send_message', 'reply', 'forward', 'trash_message', 'trash_thread', 'delete_draft',
                                'update_draft', 'delete_label', 'mark_message_spam', 'mark_thread_spam')] +
           [CALENDAR + n for n in ('respond_to_event', 'delete_event', 'update_event')] +
           [DRIVE + n for n in ('share_file', 'trash_file', 'update_file', 'create_file', 'copy_file')] +
           [SPOTIFY + n for n in ('save_to_library', 'remove_from_library', 'generate_playlist')])
