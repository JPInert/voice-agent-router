"""Mail: read, search, and save a DRAFT. There is no send.

The backend protocol has no send, reply or forward method, so the adapter cannot express one. A
draft stays in the owner's own drafts folder; sending is something the owner does by hand.
When mail comes from a hosted MCP connector instead (see hosted.py) the same rule is enforced one
layer up: its send/reply/forward/trash tools are in the BLOCKED tier.
"""


def search(backend, query):
    hits = backend.search(str(query)[:200])
    return '\n'.join(f"{h['id']}: {h['from']}: {h['subject']}" for h in hits[:10]) or 'No mail matches.'


def get(backend, id):
    m = backend.get(str(id))
    return f"From {m['from']}, subject {m['subject']}: {m['body'][:1500]}" if m else 'No such message.'


def draft(backend, to, subject, body):
    backend.create_draft(str(to), str(subject)[:200], str(body)[:4000])
    return 'Draft saved. It is waiting in the drafts folder; nothing was sent.'


def register(reg, backend):
    reg.add('mail_search', 'Search the owner\'s mail. Returns id: sender: subject lines.', {'query': str},
            lambda query: search(backend, query), source='mail')
    reg.add('mail_get', 'Read one message by id.', {'id': str}, lambda id: get(backend, id), source='mail')
    reg.add('mail_draft', 'Save a DRAFT email (never sends). Tell the owner the draft is waiting.',
            {'to': str, 'subject': str, 'body': str},
            lambda to, subject, body: draft(backend, to, subject, body), writes=True, source='mail')
