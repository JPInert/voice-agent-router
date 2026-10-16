"""Food diary: search, log, read the day, delete an entry. No create, no weight.

A bounded write: `log` needs `--units N` with N in (0, 10]. A misheard word ("2" heard as "20") must
not double the day, so an out-of-range amount is refused and the owner is asked to say it again.
The model says back what it logged ("logged 2 times <serving>") so a mishearing can be caught.
"""

COMMANDS = ('search', 'log', 'day', 'delete')


def _argv(args):
    """The argument list, or None when it is not a list. A string is refused, never split: one
    parser for the guard and the call means they cannot disagree about what a call does."""
    return [str(a) for a in args][:12] if isinstance(args, list) else None


def writes(args):
    """True when a call would change the diary. Anything that is not a clean list counts as a
    write, so the dry-run guard in ToolRegistry.run blocks it too."""
    argv = _argv(args)
    return argv is None or bool(argv) and argv[0] in ('log', 'delete')


def food(backend, args):
    argv = _argv(args)
    if argv is None:
        return 'Refused: args must be a list, e.g. ["day"].'
    if not argv or argv[0] not in COMMANDS:
        return f'Food commands: {", ".join(COMMANDS)}.'
    if argv[0] == 'log':
        try:
            units = float(argv[argv.index('--units') + 1])
        except (ValueError, IndexError):
            return 'Refused: log needs --units N.'
        if not 0 < units <= 10:
            return f'Refused: {units:g} units is outside 0 to 10. Ask them to say it again.'
    return backend.run(argv)[:3000] or 'No output.'


def register(reg, backend):
    reg.add('food', 'Food diary CLI: args = list, e.g. ["search", "oatmeal"], ["log", "--food-id", "X", '
                    '"--units", "1"], ["day"], ["delete", "<entry id>"].', {'args': list},
            lambda args=None: food(backend, args or []),
            writes=lambda a: writes(a.get('args') or []),
            source='food')
