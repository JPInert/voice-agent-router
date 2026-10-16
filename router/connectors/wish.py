"""The missing-functionality pipe: something asked for that no tool can do goes on a local list.

It builds nothing. A person reads the list and writes the tool (see "Adding a tool" in
docs/PHONE_WORKER.md). The list holds the user's own words, so it lives outside git.
"""
import json
import os
import time


def wish(path, what, heard=''):
    what = ' '.join(str(what or '').split())[:400]
    if not what:
        return 'Nothing to note.'
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps({'ts': round(time.time(), 1), 'what': what, 'heard': str(heard)[:300]}) + '\n')
    return 'Added to the wishlist.'


def register(reg, path):
    reg.add('wish', 'Note something that was asked for and no tool can do yet: what = the request and what it '
                    'would need.', {'what': str}, lambda what: wish(path, what), writes=True, source='wish')
