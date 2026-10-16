"""soul: a local memory layer for a voice assistant. See README.md."""
from .config import Config
from .core import Recent, Soul

__all__ = ['Config', 'Recent', 'Soul']
