#!/usr/bin/env python3
"""voice_mcp.py — stdio MCP server giving any Claude Code chat a mouth and ears.

Registered user-wide (`claude mcp add --scope user voice -- python3
/path/to/mcp_server/voice_mcp.py`), so EVERY interactive session gets
these tools. That has three consequences the code obeys:

  * it must start when nothing else is running — no network, no sqlite, no
    token read at import time; every side effect lives inside a tool body;
  * nothing may ever be printed to stdout (that is the MCP framing); logs go
    to stderr;
  * a tool must never hang and never raise. Services are down half the time
    (the daemon restarts on every edit), so each tool returns a short plain
    string saying so.

Every tool is a thin client of two local HTTP services:

  voice daemon  127.0.0.1:7781   /say /ask /listen /repeat /status
  curator       127.0.0.1:7782   /sessions /sessions/<name>/send /tricks

(The curator's session registry and /tricks endpoints are not part of this
extract; the tools that use them report that the endpoint is missing.)

Write endpoints on the curator need `X-Curator-Token`, read from a 0600 file
($CURATOR_TOKEN_FILE, default ~/.config/voice-agent-router/token); if it is
missing one is generated. The value is never logged or returned.

Ports come from $VOICE_PORT / $CURATOR_PORT so the whole surface can be
pointed at stub servers for testing.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

HOST = '127.0.0.1'
SAY_FIFO = '/tmp/voice-say.fifo'
SECRETS = os.path.expanduser(os.environ.get('CURATOR_TOKEN_FILE') or
                             '~/.config/voice-agent-router/token')
INBOX_DB = os.path.expanduser('~/.local/share/voice-inbox/inbox.db')

# level → the daemon's earcon names (wake/miss/sent/busy are all there is)
EARCONS = {'info': 'sent', 'warn': 'busy', 'error': 'miss'}

mcp = FastMCP('voice')


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def voice_port():
    try:
        return int(os.environ.get('VOICE_PORT') or 7781)
    except ValueError:
        return 7781


def curator_port():
    try:
        return int(os.environ.get('CURATOR_PORT') or 7782)
    except ValueError:
        return 7782


# ── token ────────────────────────────────────────────────────────────────────

def token():
    """CURATOR_TOKEN from the token file, generated once if absent.

    Last occurrence wins so a concurrent append (two hooks racing) still agrees
    with the file the curator reads the same way. Never returned to the model.
    """
    val = ''
    try:
        with open(SECRETS) as f:
            for line in f:
                line = line.strip()
                if line.startswith('CURATOR_TOKEN='):
                    val = line.split('=', 1)[1].strip().strip('"\'')
    except FileNotFoundError:
        pass
    except OSError:
        return ''
    if val:
        return val
    try:
        import secrets as _s
        val = _s.token_hex(32)
        os.makedirs(os.path.dirname(SECRETS), exist_ok=True)
        fd = os.open(SECRETS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write('CURATOR_TOKEN=%s\n' % val)
    except Exception as e:
        _log('voice-mcp: could not write CURATOR_TOKEN:', e)
        return ''
    return val


# ── plumbing ─────────────────────────────────────────────────────────────────

def _http(port, path, body=None, timeout=3.0, auth=False, method=None):
    """(ok, payload) — payload is the decoded JSON, or a spoken-plain error string."""
    url = 'http://%s:%d%s' % (HOST, port, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    if data:
        req.add_header('Content-Type', 'application/json')
    if auth:
        tok = token()
        if not tok:
            return False, 'no CURATOR_TOKEN in %s and it could not be created' % SECRETS
        req.add_header('X-Curator-Token', tok)
    who = 'voice daemon' if port == voice_port() else 'curator'
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, '%s has no %s endpoint yet (not built or not restarted)' % (who, path)
        if e.code in (401, 403):
            return False, '%s rejected the curator token' % who
        if e.code == 409:
            return False, '%s is busy with another ask' % who
        return False, '%s returned HTTP %s for %s' % (who, e.code, path)
    except urllib.error.URLError as e:
        return False, '%s is not answering on %s:%d (%s)' % (who, HOST, port, getattr(e, 'reason', e))
    except Exception as e:                       # socket.timeout lands here too
        return False, '%s did not answer %s in time (%s)' % (who, path, e)
    if not raw.strip():
        return True, {}
    try:
        return True, json.loads(raw)
    except ValueError:
        return True, {'raw': raw[:2000]}


def _fifo(line):
    """Write one command line to the daemon's FIFO. Never blocks: no reader = error.

    Opening a FIFO for writing blocks until someone opens the read end, which
    is exactly the hang this whole file is meant to avoid, so O_NONBLOCK and
    ENXIO ('no such device or address') = the daemon is not running.
    """
    try:
        fd = os.open(SAY_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False, 'the voice daemon is not running (no %s)' % SAY_FIFO
    except OSError:
        return False, 'the voice daemon is not reading its FIFO (restarting?)'
    try:
        os.write(fd, (line + '\n').encode())
    except OSError as e:
        return False, 'could not write to the voice FIFO (%s)' % e
    finally:
        os.close(fd)
    return True, 'ok'


def _dumps(obj):
    return json.dumps(obj, indent=2, default=str)


# ── speech ───────────────────────────────────────────────────────────────────

@mcp.tool()
def say(text: str) -> str:
    """Speak text out loud through the voice daemon.

    Use for a short spoken result — one or two plain sentences, no markdown,
    no code. Returns as soon as the daemon has taken the line; it does not wait
    for the speech to finish.
    """
    text = (text or '').strip()
    if not text:
        return 'nothing to say'
    ok, res = _http(voice_port(), '/say', {'text': text}, timeout=3.0)
    if ok:
        return 'spoken'
    ok, res2 = _fifo(text)                        # /say may not exist yet; the FIFO always has
    return 'spoken (via FIFO)' if ok else 'could not speak: %s' % res2


@mcp.tool()
def ask(text: str, mode: str = 'text', timeout_s: int = 8) -> str:
    """Ask the user a question out loud and wait for the spoken answer.

    Speaks `text`, then listens for one utterance without needing the wake
    word. mode="yesno" also resolves the answer to yes/no. Ask ONE short
    question; never read a list out loud. Blocks up to timeout_s + capture,
    and only one ask can be open at a time.

    Returns JSON: {heard, yes, verify} — heard is null if nothing was said.
    """
    text = (text or '').strip()
    if not text:
        return 'ask needs a question'
    if mode not in ('text', 'yesno'):
        return 'mode must be "text" or "yesno"'
    t = max(2, min(int(timeout_s or 8), 60))
    ok, res = _http(voice_port(), '/ask', {'text': text, 'mode': mode, 'timeout_s': t},
                    timeout=t + 12.0)
    return _dumps(res) if ok else 'could not ask: %s' % res


@mcp.tool()
def listen(timeout_s: int = 8) -> str:
    """Listen for one spoken utterance without asking anything first.

    Returns JSON: {heard, yes, verify}. Prefer `ask` when you have a question —
    this is for "tell me when you're ready" style waits.
    """
    t = max(2, min(int(timeout_s or 8), 60))
    ok, res = _http(voice_port(), '/listen', {'timeout_s': t}, timeout=t + 12.0)
    return _dumps(res) if ok else 'could not listen: %s' % res


@mcp.tool()
def repeat() -> str:
    """Say the last spoken line again."""
    ok, res = _http(voice_port(), '/repeat', {}, timeout=3.0)
    return 'repeated' if ok else 'could not repeat: %s' % res


@mcp.tool()
def notify(text: str, level: str = 'info') -> str:
    """Speak a short attention line with an earcon: level info | warn | error.

    For "your build finished" / "that failed" — anything the user should hear about
    without being asked a question.
    """
    text = (text or '').strip()
    if not text:
        return 'nothing to notify'
    earcon = EARCONS.get((level or 'info').lower(), 'sent')
    ok, res = _http(voice_port(), '/say', {'text': text, 'earcon': earcon}, timeout=3.0)
    if ok:
        return 'notified'
    ok, res2 = _fifo(text)
    return 'notified (via FIFO, no earcon)' if ok else 'could not notify: %s' % res2


@mcp.tool()
def status() -> str:
    """What the voice stack is doing right now.

    JSON: {voice: {speaking, last_said, last_heard} | error,
    curator: {up, sessions} | error}. Cheap; call it before assuming a tool
    failed because of you.
    """
    out = {}
    ok, res = _http(voice_port(), '/status', timeout=2.5)
    out['voice'] = res if ok else {'error': res}
    ok, res = _http(curator_port(), '/sessions', timeout=2.5)
    out['curator'] = {'up': True, 'sessions': res} if ok else {'up': False, 'error': res}
    return _dumps(out)


# ── inbox ────────────────────────────────────────────────────────────────────

def _inbox():
    os.makedirs(os.path.dirname(INBOX_DB), exist_ok=True)
    con = sqlite3.connect(INBOX_DB, timeout=5)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS entries ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, '
                'text TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)')
    return con


@mcp.tool()
def inbox_add(text: str) -> str:
    """Drop a line into the voice inbox, a queue to read later instead of hearing now.

    Use instead of `say` when it is not urgent, or when the answer is too long
    to speak.
    """
    text = (text or '').strip()
    if not text:
        return 'nothing to add'
    try:
        con = _inbox()
        with con:
            cur = con.execute('INSERT INTO entries (ts, text, consumed) VALUES (?, ?, 0)',
                              (time.time(), text))
        con.close()
        return 'added to the inbox as #%d' % cur.lastrowid
    except Exception as e:
        return 'could not write the inbox: %s' % e


@mcp.tool()
def inbox_list(n: int = 10) -> str:
    """The last n inbox entries, newest first. JSON: [{id, ts, when, text, consumed}]."""
    n = max(1, min(int(n or 10), 100))
    try:
        con = _inbox()
        rows = con.execute('SELECT id, ts, text, consumed FROM entries '
                           'ORDER BY id DESC LIMIT ?', (n,)).fetchall()
        con.close()
    except Exception as e:
        return 'could not read the inbox: %s' % e
    return _dumps([{'id': r[0], 'ts': r[1],
                    'when': time.strftime('%Y-%m-%d %H:%M', time.localtime(r[1])),
                    'text': r[2], 'consumed': bool(r[3])} for r in rows])


# ── other Claude Code sessions ───────────────────────────────────────────────

@mcp.tool()
def sessions() -> str:
    """Every Claude Code session the curator knows about.

    JSON: [{name, kind, cwd, state, last_reply_ts}]. Use it to find the chat
    that owns a job before spawning another one.
    """
    ok, res = _http(curator_port(), '/sessions', timeout=3.0)
    return _dumps(res) if ok else 'could not list sessions: %s' % res


@mcp.tool()
def send_to_session(name: str, text: str, submit: bool = False) -> str:
    """Type text into another Claude Code session by name (from `sessions`).

    submit=false leaves it in that session's prompt for the user to read and press
    Enter; submit=true sends it. submit=true runs the text as a real prompt in
    a session that skips permission prompts — only do it when the user asked for
    exactly that.
    """
    name = (name or '').strip()
    if not name:
        return 'which session?'
    if not (text or '').strip():
        return 'nothing to send'
    ok, res = _http(curator_port(), '/sessions/%s/send' % urllib.parse.quote(name, safe=''),
                    {'text': text, 'submit': bool(submit)}, timeout=5.0, auth=True)
    if not ok:
        return 'could not send to %s: %s' % (name, res)
    return 'sent to %s%s' % (name, ' and submitted' if submit else ' (not submitted)')


@mcp.tool()
def add_trick(trick: str) -> str:
    """Teach the voice daemon a new phrase → action trick (tricks.yaml).

    `trick` is a JSON object: {"name": "movie night", "phrases": ["movie
    night"], "actions": [{"ha": {"service": "light.turn_off", "entity_id":
    "light.living_room"}}, {"say": "Enjoy."}], "confirm": false}. The curator
    validates it and asks out loud before enabling it — you will not get a
    trick live without a spoken yes.
    """
    try:
        body = json.loads(trick) if isinstance(trick, str) else trick
    except ValueError as e:
        return 'trick must be a JSON object: %s' % e
    if not isinstance(body, dict) or not body.get('phrases'):
        return 'a trick needs at least {name, phrases, actions}'
    ok, res = _http(curator_port(), '/tricks', body, timeout=5.0, auth=True)
    return _dumps(res) if ok else 'could not add the trick: %s' % res

if __name__ == '__main__':
    try:
        mcp.run('stdio')
    except (BrokenPipeError, KeyboardInterrupt):
        pass
