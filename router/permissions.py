"""Shell permission policy for voice-driven agent sessions.

Read-only commands run silently; anything that writes, installs, deletes or
leaves the machine is read out loud and needs a spoken yes. Deny-by-default:
if a command cannot be PROVEN read-only, it asks.

`bash_is_readonly(cmd)` is the whole public surface. It is called from the
Agent SDK `can_use_tool` callback in ladder.py for every Bash call a worker
session makes.
"""
import os
import re
import shlex

READONLY_CMDS = {
    'ls', 'cat', 'head', 'tail', 'wc', 'stat', 'file', 'realpath', 'readlink',
    'basename', 'dirname', 'pwd', 'echo', 'printf', 'date', 'uptime', 'uname',
    'hostname', 'whoami', 'id', 'env', 'printenv', 'which', 'type', 'command',
    'df', 'du', 'free', 'lsblk', 'blkid', 'findmnt', 'mount', 'lscpu', 'lsusb',
    'lspci', 'lsmod', 'sensors', 'vmstat', 'iostat', 'mpstat', 'nproc',
    'ps', 'pgrep', 'top', 'pidof', 'lsof', 'ss', 'ip', 'ping', 'dig', 'host',
    'nslookup', 'traceroute', 'arp', 'iw', 'iwconfig', 'ethtool',
    'grep', 'egrep', 'fgrep', 'rg', 'find', 'sort', 'uniq', 'cut', 'tr',
    'awk', 'sed', 'jq', 'yq', 'column', 'diff', 'cmp', 'md5sum', 'sha256sum',
    'xrandr', 'xdpyinfo', 'xdotool', 'wmctrl', 'xprop', 'xwininfo',
    'lpstat', 'lpq', 'journalctl', 'dmesg', 'true', 'false', 'sleep', 'seq',
    'git', 'kitty',
}
# Fetching is silent by policy (the web is read-only from here), which makes "download and execute" the
# thing to guard: an interpreter as a pipeline target reads its program from
# stdin. None of these can ever run silently.
INTERPRETERS = {'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'python', 'python3',
                'perl', 'ruby', 'node', 'php', 'lua', 'Rscript', 'osascript',
                'xargs', 'eval', 'source', 'exec'}
# Retrieval verbs — silent unless an argument below turns the
# call into a submission or an upload.
FETCH_CMDS = {'curl', 'wget', 'http', 'https', 'xh', 'aria2c', 'lynx', 'w3m'}
# flags that mean "this sends something", not "this gets something"
FETCH_WRITE_FLAGS = (
    '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode',
    '--data-ascii', '--json', '-F', '--form', '--form-string',
    '-T', '--upload-file', '-u', '--user', '--netrc', '--netrc-file',
    '--post-data', '--post-file', '--body-data', '--body-file',
    '--http-user', '--http-password', '--ftp-user', '--ftp-password',
)
WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
# argv[0] -> the only subcommands that are read-only
READONLY_SUBS = {
    'pactl': {'list', 'info', 'stat', 'get-default-sink', 'get-default-source',
              'get-sink-volume', 'get-source-volume', 'get-sink-mute',
              'get-source-mute'},
    'bluetoothctl': {'show', 'info', 'devices', 'list', 'paired-devices'},
    'systemctl': {'status', 'show', 'list-units', 'list-unit-files',
                  'list-timers', 'is-active', 'is-enabled', 'is-failed', 'cat'},
    'nmcli': {'device', 'connection', 'general', 'radio', 'networking'},
    'git': {'status', 'log', 'diff', 'show', 'branch', 'remote', 'rev-parse',
            'describe', 'blame', 'ls-files', 'config'},
    'flatpak': {'list', 'info', 'search', 'history'},
    'apt': {'list', 'show', 'search', 'policy'},
    'apt-cache': {'show', 'search', 'policy', 'depends'},
    'dpkg': {'-l', '-L', '-s', '-S', '--list', '--status'},
    'docker': {'ps', 'images', 'logs', 'inspect', 'stats'},
    'kitty': {'@'},
}
# argv[0]s whose subcommand must NOT appear anywhere: they always change things
NEVER_SILENT = {'rm', 'rmdir', 'mv', 'cp', 'dd', 'mkfs', 'fdisk', 'parted',
                'chmod', 'chown', 'ln', 'install', 'tee', 'truncate',
                'shutdown', 'reboot', 'poweroff', 'halt', 'kill', 'pkill',
                'killall', 'sudo', 'su', 'doas', 'passwd', 'usermod',
                'ssh', 'scp', 'rsync', 'nc', 'ncat',
                'pip', 'pip3', 'npm', 'npx', 'yarn', 'cargo', 'go',
                'crontab', 'at', 'systemd-run', 'mail', 'sendmail'} | INTERPRETERS
WRITEY_SHELL = re.compile(r'(^|\s)(>|>>|\|\s*tee\b)')


def _fetch_is_retrieval(argv):
    """A fetch verb: silent when it GETS, spoken when it SENDS.

    Asks when the command carries a body/form/upload, logs in with -u/--user,
    or names a write method — `curl -d`, `curl -X POST`, `wget --post-data`,
    `http POST url`, `curl -T file`. Download-and-execute is caught separately:
    an interpreter as a pipeline target is in NEVER_SILENT.
    """
    for i, a in enumerate(argv[1:]):
        low = a.lower()
        for f in FETCH_WRITE_FLAGS:
            # exact flag, or --flag=value; short flags may be bundled (-sd)
            if low == f or low.startswith(f + '='):
                return False
        if a.startswith('-') and not a.startswith('--') and len(a) > 1:
            if any(c in a[1:] for c in ('d', 'F', 'T', 'u')):
                return False              # bundled short form, e.g. -sSd
        if low in ('-x', '--request', '--method') and i + 2 <= len(argv) - 1:
            if argv[i + 2].upper() in WRITE_METHODS:
                return False
        if low.startswith(('--request=', '--method=', '-x=')):
            if low.split('=', 1)[1].upper() in WRITE_METHODS:
                return False
        if a.upper() in WRITE_METHODS:    # httpie: `http POST example.com`
            return False
        if '=' in a and not a.startswith('-') and '://' not in a:
            return False                  # httpie body/form field key=value
    return True


# Only these may prefix a command as VAR=x. PAGER / GIT_PAGER / LD_PRELOAD / EDITOR etc.
# turn a read verb into "run this program".
SAFE_ENV_PREFIX = {'LC_ALL', 'LANG', 'LANGUAGE', 'TZ', 'COLUMNS', 'LINES', 'NO_COLOR', 'TERM',
                   'SYSTEMD_COLORS'}
# Redirects that write nothing: to /dev/null, and fd duplication (2>&1).
_NULL_REDIRECT = re.compile(r'(?<![^\s;&|])(?:\d|&)?>>?\s*/dev/null(?![^\s;&|])|(?<![^\s;&|])\d?>&\d(?![^\s;&|])')
_SEPARATORS = {'&&', '||', ';', '|'}
_PUNCT = set('();<>|&')
# git subcommands, each with the options that make it write
_GIT_WRITE_OPTS = {
    'branch': {'-d', '-D', '-m', '-M', '-c', '-C', '-f', '-u', '--delete', '--move', '--copy',
               '--force', '--unset-upstream', '--edit-description', '--track', '--no-track',
               '--create-reflog', '--set-upstream-to'},
    'diff': {'--output'}, 'log': {'--output'}, 'show': {'--output'},
}
# `git branch <name>` CREATES a branch; a positional is only a filter under a list-mode flag
_GIT_BRANCH_LIST = {'-l', '--list', '--contains', '--no-contains', '--merged', '--no-merged',
                    '--points-at'}
_GIT_CONFIG_READ = {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '-l', '--list'}
_GIT_CONFIG_WRITE = {'--unset', '--unset-all', '--add', '--replace-all', '-e', '--edit',
                     '--rename-section', '--remove-section'}


def _opt(a):
    return a.split('=', 1)[0]


def _git_is_readonly(argv):
    args = argv[1:]
    while args and args[0] in ('-C', '--no-pager', '-P'):
        args = args[2:] if args[0] == '-C' else args[1:]
    if not args or args[0] not in READONLY_SUBS['git']:
        return False                                  # -c, --exec-path, unknown verbs: ask
    sub, rest = args[0], args[1:]
    opts = {_opt(a) for a in rest if a.startswith('-')}
    pos = [a for a in rest if not a.startswith('-')]
    if opts & _GIT_WRITE_OPTS.get(sub, set()):
        return False
    if sub == 'branch' and pos and not (opts & _GIT_BRANCH_LIST):
        return False
    if sub == 'remote' and pos and pos[0] not in ('show', 'get-url'):
        return False
    if sub == 'config':
        if opts & _GIT_CONFIG_WRITE:
            return False
        if not (opts & _GIT_CONFIG_READ or (pos and pos[0] in ('get', 'list'))):
            return False
    return True


def _sed_write_substitution(program):
    """Find e/w among s-command flags, including combined flags and alternate delimiters.

    Scan escaped delimiters linearly. An incomplete s-command is not provably read-only.
    """
    for i in range(len(program) - 1):
        if program[i] != 's':
            continue
        # A letter right before `s` means it is inside a word (`/errors/p`), not a command —
        # sed rejects `ps/..`. Except I and M, the address-regex modifiers (`/x/Is/a/b/e` runs).
        if i and program[i - 1].isalpha() and program[i - 1] not in 'IM':
            continue
        delim = program[i + 1]
        if delim.isalnum() or delim.isspace() or delim == '\\':
            continue
        j = i + 2
        for part in range(2):         # regex close, then replacement close
            while j < len(program):
                if program[j] == '\\':
                    j += 2
                elif part == 0 and program[j] == '[':
                    # the delimiter is LITERAL inside a bracket expression: `s/[/]/x/ge`
                    k = j + 1
                    if k < len(program) and program[k] == '^':
                        k += 1
                    if k < len(program) and program[k] == ']':
                        k += 1
                    while k < len(program) and program[k] != ']':
                        k += (program.index(':]', k) + 2 if program.startswith('[:', k)
                              and ':]' in program[k:] else k + 1) - k
                    if k >= len(program):
                        return True
                    j = k + 1
                elif program[j] == delim:
                    j += 1
                    break
                else:
                    j += 1
            else:
                return True
        while j < len(program) and program[j].isalnum():
            if program[j] in 'eEwW':
                return True
            j += 1
    return False


def _args_ok(cmd, argv):
    """Per-command argument rules for the entries in READONLY_CMDS that CAN write."""
    args = argv[1:]
    opts = {_opt(a) for a in args if a.startswith('-')}
    pos = [a for a in args if not a.startswith('-')]
    if cmd == 'find':
        return not any(a in ('-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint',
                             '-fprint0', '-fprintf', '-fls') for a in args)
    if cmd == 'sed':
        # w/W write a file and e runs a command — after an address (`1w f`, `$e cmd`) or as an
        # s///w flag. Checked in EVERY program text, option values included; any w/W/e that is
        # not part of a longer word asks (conservative: `s/a/w-b/` asks too). Substitution flags
        # can be combined (`s/a/b/ge`, `s/a/b/gw file`), so check the flags after the THIRD
        # delimiter separately. -f hides the program and always asks.
        # Short options BUNDLE (`-ri`, `-nes/a/b/w out`): walk each one a letter at a time.
        progs = []
        for a in args:
            if a.startswith('--'):
                if a.startswith('--expression='):
                    progs.append(a.split('=', 1)[1])
                elif _opt(a) not in ('--quiet', '--silent', '--regexp-extended', '--separate',
                                     '--unbuffered', '--null-data', '--posix', '--sandbox',
                                     '--debug', '--expression'):
                    return False                           # --in-place, --file, anything unknown
            elif a.startswith('-') and len(a) > 1:
                for k, c in enumerate(a[1:], 1):
                    if c in 'nErsuz':
                        continue
                    if c == 'e':
                        if a[k + 1:]:
                            progs.append(a[k + 1:])        # the rest of the token IS the program
                        break
                    if c == 'l' and a[k + 1:].isdigit():
                        break
                    return False                           # i, f, and anything unknown
            else:
                progs.append(a)
        return not any(re.search(r'(?:^|[^A-Za-z\\])[wWe](?:[^A-Za-z]|$)', t)
                       or _sed_write_substitution(t) for t in progs)
    if cmd == 'awk':
        # awk here is mawk (`-W exec file` runs a program); gawk adds `@include "inplace"`
        if any(a.startswith(('-f', '-e', '-i', '-l', '-E', '-W', '--')) for a in args):
            return False                                   # program from a file / an option
        return not any(re.search(r'system|getline|\||>|fflush|close\s*\(|@', a) for a in pos)
    if cmd == 'sort':
        return not (opts & {'-o', '--output', '--compress-program'}) and not any(
            a.startswith('-') and not a.startswith('--') and 'o' in a[1:] for a in args)
    if cmd == 'uniq':
        return len(pos) <= 1                           # a 2nd operand is the OUTPUT file
    if cmd == 'yq':
        return not (opts & {'--inplace'}) and not any(
            a.startswith('-') and not a.startswith('--') and 'i' in a[1:] for a in args)
    if cmd == 'rg':
        return not (opts & {'--pre'})
    if cmd == 'env':
        # `env rm x` runs rm; `env -S'rm x'` runs it too, hidden inside one option token
        return not pos and not any(a.startswith(('-S', '--split-string')) or
                                   (a.startswith('-') and not a.startswith('--') and 'S' in a)
                                   for a in args)
    if cmd == 'xprop':
        return not (opts & {'-set', '-remove'})
    if cmd == 'command':
        return bool(opts & {'-v', '-V'})               # `command rm x` runs rm
    if cmd in ('mount', 'hostname'):
        return not pos and not (opts & {'-F', '--file', '-b', '--boot', '-a', '--all'})
    if cmd == 'date':
        return not (opts & {'-s', '--set'})
    if cmd == 'journalctl':
        return not any(o.startswith('--vacuum') for o in opts) and not (
            opts & {'--rotate', '--flush', '--sync', '--relinquish-var', '--smart-relinquish-var',
                    '--setup-keys', '--update-catalog'})
    if cmd == 'dmesg':
        return not (opts & {'-C', '-c', '-D', '-E', '-n', '--clear', '--read-clear',
                            '--console-off', '--console-on', '--console-level'})
    if cmd == 'ip':
        return len(pos) < 2 or pos[1] in ('show', 'list', 'ls', 'get', 'lst')
    if cmd == 'iw':
        return not set(pos) & {'set', 'connect', 'disconnect', 'del', 'add', 'join', 'leave',
                               'switch', 'cac', 'offchannel', 'trigger', 'abort'}
    if cmd == 'iwconfig':
        return len(pos) <= 1
    if cmd == 'arp':
        return not (opts & {'-d', '-s', '-f', '--delete', '--set', '--file'})
    if cmd == 'ethtool':
        return all(o in {'-i', '--driver', '-S', '--statistics', '-k', '--show-features',
                         '-a', '--show-pause', '-c', '--show-coalesce', '-g', '--show-ring',
                         '-l', '--show-channels', '-m', '--module-info', '-T',
                         '--show-time-stamping', '-P', '--show-permaddr', '--show-eee',
                         '--show-fec'} for o in opts)
    if cmd == 'xrandr':
        return all(o in {'-q', '--query', '--listmonitors', '--listactivemonitors',
                         '--current', '--verbose', '--prop', '--props'} for o in opts) and not pos
    if cmd == 'wmctrl':
        return all(o in {'-l', '-d', '-m', '-p', '-G', '-x'} for o in opts) and not pos
    if cmd == 'xdotool':
        # xdotool CHAINS: `search --name x windowkill` runs the kill. Every word that could be
        # a verb must be a getter; search patterns are what makes this a denylist.
        return bool(pos) and pos[0] in _XDOTOOL_READ and not set(pos) & _XDOTOOL_WRITE
    return True


_XDOTOOL_READ = {'getactivewindow', 'getwindowfocus', 'getwindowname', 'getwindowpid',
                 'getwindowgeometry', 'getwindowclassname', 'getmouselocation',
                 'getdisplaygeometry', 'search', 'get_desktop', 'get_num_desktops',
                 'get_desktop_for_window', 'get_desktop_viewport', 'version'}
_XDOTOOL_WRITE = {'key', 'keydown', 'keyup', 'type', 'click', 'mousedown', 'mouseup',
                  'mousemove', 'mousemove_relative', 'windowkill', 'windowclose',
                  'windowquit', 'windowactivate', 'windowfocus', 'windowmap', 'windowunmap',
                  'windowminimize', 'windowmove', 'windowsize', 'windowraise', 'windowlower',
                  'windowstate', 'windowreparent', 'set_window', 'set_desktop',
                  'set_desktop_for_window', 'set_num_desktops', 'set_desktop_viewport',
                  'exec', 'behave', 'behave_screen_edge', 'sleep'}
_KITTY_READ = {'ls', 'get-text', 'get-colors'}
_NMCLI_READ = {'show', 'status', 'list'}


def _segments(command):
    """Shell-aware split into argv lists, or None if anything is not provably plain.

    Tokenises with shlex's punctuation mode, so `a|b` inside quotes is a word, not a pipe.
    Only && || ; | may join commands; &, (, ), <, > and every other operator reject the
    whole line — background jobs, subshells, process substitution and redirects are not
    things this checker can see through. A NEWLINE is a command separator to bash but plain
    whitespace to shlex, so any newline rejects too (09-24 review: `echo hi⏎rm -rf x` read
    as one echo)."""
    if '\n' in command or '\r' in command:
        return None
    command = _NULL_REDIRECT.sub(' ', command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in toks:
        if t in _SEPARATORS:
            segs.append(cur)
            cur = []
        elif t and set(t) <= _PUNCT:
            return None
        else:
            cur.append(t)
    segs.append(cur)
    return segs


def bash_is_readonly(command):
    """True only if every segment of the command line is provably read-only.
    Deny-by-default: anything this cannot PROVE read-only asks by voice."""
    if not command or not isinstance(command, str):
        return False
    if '$(' in command or '`' in command or '<(' in command or '>(' in command:
        return False                       # substitution hides the verb
    segs = _segments(command)
    if segs is None:
        return False
    for argv in segs:
        if not argv:
            continue
        while argv and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', argv[0]):
            if argv[0].split('=', 1)[0] not in SAFE_ENV_PREFIX:
                return False               # PAGER=, LD_PRELOAD=, GIT_*=: runs a program
            argv = argv[1:]
        if not argv:
            return False
        cmd = os.path.basename(argv[0])
        if cmd in NEVER_SILENT:
            return False
        if cmd in FETCH_CMDS:
            if not _fetch_is_retrieval(argv):
                return False
            continue
        if cmd == 'git':
            if not _git_is_readonly(argv):
                return False
            continue
        if cmd == 'kitty':
            if argv[1:2] != ['@'] or len(argv) < 3 or argv[2] not in _KITTY_READ:
                return False
            continue
        if cmd == 'nmcli':
            pos = [a for a in argv[1:] if not a.startswith('-')]
            if not pos or pos[0] not in READONLY_SUBS['nmcli']:
                return False
            if len(pos) > 1 and pos[1] not in _NMCLI_READ and not (pos[0] == 'radio' and len(pos) == 2):
                return False
            continue
        if cmd == 'dpkg':
            if not argv[1:] or not all(a in READONLY_SUBS['dpkg'] for a in argv[1:] if a.startswith('-')):
                return False
            continue
        if cmd in READONLY_SUBS:
            subs = READONLY_SUBS[cmd]
            rest = [a for a in argv[1:] if not a.startswith('-')] or argv[1:]
            if not rest or rest[0] not in subs:
                return False
            continue
        if cmd not in READONLY_CMDS or not _args_ok(cmd, argv):
            return False
    return True


# WebFetch/WebSearch retrieve; they cannot submit or execute, so they are
# silent.
SILENT_TOOLS = {'Read', 'Glob', 'Grep', 'TodoWrite', 'NotebookRead',
                'WebFetch', 'WebSearch',
                'mcp__voice__say', 'mcp__voice__notify', 'mcp__voice__status',
                'mcp__voice__ask', 'mcp__voice__snapshot'}


def describe_tool(tool_name, data):
    if tool_name == 'Bash':
        return (data.get('command') or '')[:120]
    for k in ('file_path', 'path', 'url', 'pattern', 'command', 'query'):
        if data.get(k):
            return f'{k}={str(data[k])[:100]}'
    return ''


def speak_cmd(cmd):
    """A shell command the TTS can read out without sounding deranged (a spoken
    "tilde slash" is useless as a permission prompt). Speech only; logs keep the raw text."""
    s = cmd
    s = re.sub(r'\s*2>\s*/dev/null', '', s)
    s = re.sub(r'\s*2>&1', '', s)
    s = s.replace('&&', ', then ').replace('||', ', otherwise ')
    s = re.sub(r'(?:^|(?<=\s))~/', 'home ', s)
    s = re.sub(r'(?:^|(?<=\s))~(?=\s|$)', 'my home folder', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def speak_tool(tool_name):
    """A tool name the TTS can read out without sounding deranged. The non-Bash
    confirm once spoke the RAW 'mcp__claude_ai_Spotify__search. OK?' out loud.
    Speech only; the log keeps the raw name."""
    s = re.sub(r'^mcp__', '', tool_name)
    s = re.sub(r'^claude_ai_', '', s)
    s = s.replace('__', ' ').replace('_', ' ')
    return re.sub(r'\s+', ' ', s).strip()
