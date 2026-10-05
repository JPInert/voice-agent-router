"""tricks.py — tier 0: the file-backed trick list ("phrase → actions"), no model.

One schema, two readers: the voice daemon (matches every utterance in `route()`
before anything that costs a model call) and the curator's `add_trick` endpoint.
Nothing here talks to the mic, Home Assistant or Kodi directly — the daemon hands
`run()` a context object with the callables.

Why a file and not code: the complicated intents (`kodi_play`, COMFORT_RE,
"good night") are programs and stay programs. The *simple* ones — a phrase that
means one HA service call, one script, one spoken line — should not need a code
edit and a daemon restart, and the LLM should be able to read the list.

    tricks = load(path)                  # bad entries are logged and skipped
    t = match("movie night", tricks)     # whole-utterance, case-insensitive
    run(t, ctx)                          # ctx: see TrickCtx below

Schema (a YAML list; every key optional except name + actions + one matcher):

    - name: movie night                  # str, required — what /status and the CLI show
      phrases: ["movie night", "movie time"]   # whole utterance, case/punctuation-insensitive
      regex: "^movie (night|time)$"      # str or list of str, searched (not anchored for you)
      actions:                           # required, non-empty, in order
        - ha: {service: light.turn_off, entity_id: light.living_room}
        - ha_assist: "turn off the fan"
        - kodi: "next episode"           # or {play: "silo"} / {transport: stop|pause|play}
        - shell: ["notify-send", "hi"]   # argv list; forced confirm unless readonly: true
        - script: dim.sh                 # a file in the scripts dir (basename only)
        - say: "Enjoy."
        - curator: "turn the desk light off"   # hand it to curator.py as an utterance
      confirm: false                     # ask by voice before running (yes/no)
      readonly: true                     # shell/script only reads → no forced confirm
      enabled: true                      # false = parsed, listed, never matched

`shell` and `script` get `confirm: true` forced unless the trick says
`readonly: true` — the same rule as the agent tiers: read-only runs silently,
anything that writes/installs/deletes/sends asks out loud.
"""
import os, re, shlex, subprocess, time

try:
    import yaml
except Exception:                     # PyYAML missing → no tricks, daemon unaffected
    yaml = None

ACTION_KINDS = ('ha', 'ha_assist', 'kodi', 'shell', 'say', 'script', 'curator')
SHELL_TIMEOUT = 20                    # s per shell/script action
SCRIPTS_DIRNAME = 'scripts'


def _default_log(*a):
    print('tricks:', *(str(x) for x in a))


_log = _default_log


def set_logger(fn):
    """The daemon points this at its own log() so trick errors land in its log."""
    global _log
    _log = fn or _default_log


class TrickError(ValueError):
    pass


class Trick:
    __slots__ = ('name', 'phrases', 'regexes', 'actions', 'confirm', 'readonly',
                 'enabled', 'source', 'matched')

    def __init__(self, name, phrases, regexes, actions, confirm, readonly, enabled, source):
        self.name, self.phrases, self.regexes = name, phrases, regexes
        self.actions, self.confirm, self.readonly = actions, confirm, readonly
        self.enabled, self.source = enabled, source
        self.matched = None            # the re.Match of the last match(), for logging

    def __repr__(self):
        return f'<Trick {self.name!r} {len(self.actions)} action(s)' + ('' if self.enabled else ' DISABLED') + '>'

    def kinds(self):
        return [k for k, _ in self.actions]

    def as_dict(self):
        """Round-trips back to YAML (the CLI's `list`/`add` and GET /tricks)."""
        d = {'name': self.name}
        if self.phrases:
            d['phrases'] = list(self.phrases)
        if self.regexes:
            d['regex'] = [r.pattern for r in self.regexes]
        d['actions'] = [{k: v} for k, v in self.actions]
        if self.confirm:
            d['confirm'] = True
        if self.readonly:
            d['readonly'] = True
        d['enabled'] = self.enabled
        return d


# ── normalisation / matching ────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s']+")
_WS_RE = re.compile(r'\s+')


def norm(text):
    """Whole-utterance comparison form: lowercase, punctuation dropped, spaces collapsed.
    Whisper writes 'Movie night.' / 'Movie, night!' — the phrase list must not care."""
    return _WS_RE.sub(' ', _PUNCT_RE.sub(' ', (text or '').lower())).strip()


# ── loading / validation ────────────────────────────────────────────────────

def _need_str(v, what):
    if not isinstance(v, str) or not v.strip():
        raise TrickError(f'{what} must be a non-empty string')
    return v.strip()


def _check_action(kind, val):
    """Normalise one action's payload and reject what run() could not execute."""
    if kind == 'ha':
        if not isinstance(val, dict):
            raise TrickError("ha: expects a mapping, e.g. {service: light.turn_on, entity_id: light.x}")
        val = dict(val)
        svc = val.pop('service', None)
        dom = val.pop('domain', None)
        if svc and '.' in str(svc) and not dom:
            dom, svc = str(svc).split('.', 1)
        if not dom or not svc:
            raise TrickError("ha: needs service: 'domain.service' (or domain: + service:)")
        return {'domain': str(dom), 'service': str(svc), 'data': val}
    if kind == 'ha_assist':
        return _need_str(val, 'ha_assist')
    if kind == 'kodi':
        if isinstance(val, str):
            return {'play': _need_str(val, 'kodi')}
        if isinstance(val, dict) and (val.get('play') or val.get('transport')):
            out = {}
            if val.get('play'):
                out['play'] = _need_str(val['play'], 'kodi.play')
            if val.get('transport'):
                t = _need_str(val['transport'], 'kodi.transport')
                if t not in ('stop', 'pause', 'play'):
                    raise TrickError('kodi.transport must be stop, pause or play')
                out['transport'] = t
            return out
        raise TrickError('kodi: expects a title string, {play: …} or {transport: stop|pause|play}')
    if kind == 'shell':
        if isinstance(val, str):
            # a bare string is convenient but ambiguous; split it ourselves, never via a shell
            val = shlex.split(val)
        if not isinstance(val, list) or not val or not all(isinstance(x, str) for x in val):
            raise TrickError('shell: expects an argv LIST of strings (no shell is used)')
        return list(val)
    if kind == 'say':
        return _need_str(val, 'say')
    if kind == 'script':
        if isinstance(val, dict):
            name, args = val.get('name'), val.get('args') or []
        else:
            name, args = val, []
        name = _need_str(name, 'script')
        if os.path.sep in name or name.startswith('.'):
            raise TrickError('script: basename only, it must live in the scripts dir')
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise TrickError('script.args: must be a list of strings')
        return {'name': name, 'args': list(args)}
    if kind == 'curator':
        return _need_str(val, 'curator')
    raise TrickError(f'unknown action kind {kind!r} (known: {", ".join(ACTION_KINDS)})')


def build(entry, source='?'):
    """One YAML mapping → Trick. Raises TrickError with a human reason."""
    if not isinstance(entry, dict):
        raise TrickError('each trick must be a mapping')
    name = _need_str(entry.get('name'), 'name')
    phrases = entry.get('phrases') or []
    if isinstance(phrases, str):
        phrases = [phrases]
    if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
        raise TrickError('phrases: must be a list of strings')
    phrases = [norm(p) for p in phrases if norm(p)]
    rx = entry.get('regex') or []
    if isinstance(rx, str):
        rx = [rx]
    if not isinstance(rx, list) or not all(isinstance(p, str) for p in rx):
        raise TrickError('regex: must be a string or a list of strings')
    regexes = []
    for p in rx:
        try:
            regexes.append(re.compile(p, re.I))
        except re.error as e:
            raise TrickError(f'regex {p!r} does not compile: {e}')
    if not phrases and not regexes:
        raise TrickError('needs phrases: or regex:')
    raw_actions = entry.get('actions')
    if not isinstance(raw_actions, list) or not raw_actions:
        raise TrickError('actions: must be a non-empty list')
    actions = []
    for a in raw_actions:
        if not isinstance(a, dict) or len(a) != 1:
            raise TrickError('each action is a one-key mapping, e.g. - say: "hi"')
        kind, val = next(iter(a.items()))
        actions.append((kind, _check_action(kind, val)))
    confirm = bool(entry.get('confirm', False))
    readonly = bool(entry.get('readonly', False))
    if any(k in ('shell', 'script') for k, _ in actions) and not readonly:
        confirm = True                      # writes ask out loud
    for k in entry:
        if k not in ('name', 'phrases', 'regex', 'actions', 'confirm', 'readonly', 'enabled', 'note'):
            raise TrickError(f'unknown key {k!r}')
    return Trick(name, phrases, regexes, actions, confirm, readonly,
                 bool(entry.get('enabled', True)), source)


def load(path):
    """Parse the YAML file → [Trick]. Bad entries are logged and skipped; a file that
    will not parse at all raises TrickError (the daemon keeps its last good set)."""
    if yaml is None:
        raise TrickError('PyYAML is not installed')
    with open(path) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TrickError('the file must be a YAML list of tricks')
    out, names = [], set()
    for i, entry in enumerate(raw):
        try:
            t = build(entry, source=f'{os.path.basename(path)}#{i}')
            if t.name.lower() in names:
                raise TrickError(f'duplicate name {t.name!r}')
            names.add(t.name.lower())
            out.append(t)
        except TrickError as e:
            _log(f'trick #{i} skipped: {e}')
        except Exception as e:                      # never let one bad line kill the list
            _log(f'trick #{i} skipped: {e.__class__.__name__}: {e}')
    return out


def match(text, tricks):
    """First enabled trick whose phrase (whole utterance) or regex (searched) matches."""
    if not text or not tricks:
        return None
    n = norm(text)
    if not n:
        return None
    for t in tricks:
        if not t.enabled:
            continue
        if n in t.phrases:
            t.matched = None
            return t
        for r in t.regexes:
            m = r.search(text) or r.search(n)
            if m:
                t.matched = m
                return t
    return None


class Loader:
    """mtime-cached view of the file. The daemon calls poll() about once a second from
    the wake loop (one os.stat, no inotify); the last good set survives a broken edit."""

    def __init__(self, path):
        self.path = path
        self.tricks = []
        self.mtime = None
        self.error = None
        self.loaded_at = 0.0

    def poll(self, force=False):
        try:
            st = os.stat(self.path)
        except OSError:
            if self.mtime is not None or force:
                self.mtime, self.tricks, self.error = None, [], 'no tricks file'
                _log(f'tricks: {self.path} is gone — 0 tricks')
            return self.tricks
        key = (st.st_mtime_ns, st.st_size)
        if key == self.mtime and not force:
            return self.tricks
        try:
            got = load(self.path)
        except Exception as e:
            self.error = f'{e.__class__.__name__}: {e}'
            _log(f'tricks: {self.path} did not load ({self.error}) — keeping {len(self.tricks)} loaded')
            self.mtime = key                       # don't retry the same broken file every second
            return self.tricks
        self.mtime, self.tricks, self.error, self.loaded_at = key, got, None, time.time()
        _log(f'tricks: {len(got)} loaded from {self.path}' +
             (' (' + ', '.join(t.name for t in got) + ')' if got else ''))
        return self.tricks


# ── running ─────────────────────────────────────────────────────────────────

class TrickCtx:
    """What run() is allowed to touch. The daemon builds one of these per utterance;
    the CLI can pass its own (say=print, confirm=lambda …: input()) to dry-run a trick."""

    def __init__(self, say=None, earcon=None, ha=None, kodi_play=None, duck=None,
                 confirm=None, curator=None, log=None, scripts_dir=None):
        self.say = say or (lambda t: None)
        self.earcon = earcon or (lambda n: None)
        self.ha = ha
        self.kodi_play = kodi_play
        self.duck = duck
        self.confirm = confirm or (lambda prompt: False)
        self.curator = curator or (lambda text: None)
        self.log = log or _log
        self.scripts_dir = scripts_dir


def run(trick, ctx):
    """Execute a trick's actions in order. Returns (ok, note) — note is for the log.
    Anything a single action raises is caught: the rest of the trick still runs."""
    if trick.confirm:
        prompt = f'{trick.name}?'
        if not ctx.confirm(prompt):
            ctx.log(f'trick {trick.name!r}: not confirmed')
            return False, 'not confirmed'
        ctx.log(f'trick {trick.name!r}: confirmed')
    spoke, done = False, []
    for kind, val in trick.actions:
        try:
            spoke |= bool(_run_action(kind, val, ctx))
            done.append(kind)
        except Exception as e:
            ctx.log(f'trick {trick.name!r} action {kind} failed: {e.__class__.__name__}: {e}')
    if not spoke:
        ctx.earcon('sent')                 # silent trick still acknowledges, like the HA path
    return True, ','.join(done)


def _run_action(kind, val, ctx):
    """True if this action spoke (so run() knows whether to chirp)."""
    if kind == 'say':
        ctx.say(val)
        return True
    if kind == 'ha':
        if ctx.ha is None:
            raise TrickError('no Home Assistant configured')
        ctx.ha.call(val['domain'], val['service'], val['data'])
        ctx.log(f"trick ha: {val['domain']}.{val['service']} {val['data']}")
        return False
    if kind == 'ha_assist':
        if ctx.ha is None:
            raise TrickError('no Home Assistant configured')
        ok, speech = ctx.ha.process(val)
        ctx.log(f'trick ha_assist {val!r}: matched={ok} {speech!r}')
        if speech:
            ctx.say(speech)
            return True
        return False
    if kind == 'kodi':
        if ctx.kodi_play is None:
            raise TrickError('kodi_play is not available')
        if val.get('transport'):
            if ctx.duck is not None:
                ctx.duck.transport(val['transport'])
            return False
        reply = ctx.kodi_play.handle_intent(val['play'])
        if ctx.duck is not None and getattr(ctx.kodi_play, 'last_played', None):
            ctx.duck.paused = False           # a new item is playing: never "resume" it
        ctx.say(reply)
        return True
    if kind in ('shell', 'script'):
        if kind == 'shell':
            argv = list(val)
        else:
            if not ctx.scripts_dir:
                raise TrickError('no scripts dir configured')
            p = os.path.join(ctx.scripts_dir, val['name'])
            if not os.path.exists(p):
                raise TrickError(f'script {val["name"]} not found in {ctx.scripts_dir}')
            argv = [p] + val['args']
        t0 = time.time()
        r = subprocess.run(argv, capture_output=True, text=True, timeout=SHELL_TIMEOUT)
        out = (r.stdout or '').strip()
        ctx.log(f'trick {kind} {argv!r} → rc={r.returncode} {time.time()-t0:.1f}s {out[:120]!r}'
                + (f' err={r.stderr.strip()[:120]!r}' if r.returncode else ''))
        return False
    if kind == 'curator':
        ctx.curator(val)
        return False
    raise TrickError(f'unknown action kind {kind!r}')


if __name__ == '__main__':                 # `python3 -m router.tricks <file> [text]` = parse check
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else 'examples/tricks.yaml'
    ts = load(p)
    for t in ts:
        print(f'{t.name:24} {"on " if t.enabled else "off"} phrases={t.phrases} regex={[r.pattern for r in t.regexes]} '
              f'actions={t.kinds()}' + (' confirm' if t.confirm else ''))
    if len(sys.argv) > 2:
        print('match:', match(sys.argv[2], ts))
