"""Offline tests: no network, no API key, no model download. Run from the directory that CONTAINS soul/:

    python3 -m unittest discover -s soul/tests -t .

Importing this package puts that directory on sys.path, so `cd soul && python3 -m unittest` works too.
"""
import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
