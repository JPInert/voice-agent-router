"""Configuration: one dataclass, loaded from a TOML file (stdlib `tomllib`, Python 3.11+).

Nothing about the household lives in code. Names, wake word, spoken style, what the house can do and
every policy number (how rarely it speaks first, how similar is "the same") are fields here. See
`soul.example.toml` for a documented file; every field has a default, so a missing file is fine.
"""
import dataclasses
import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class Config:
    # --- where things live -----------------------------------------------------------
    data_dir: str = '~/.local/share/soul'
    model_dir: str = ''                      # '' = <data_dir>/models/bge-small-en-v1.5
    # --- who and how it talks --------------------------------------------------------
    user_name: str = 'the user'              # how prompts refer to the person it serves
    wake_word: str = 'computer'              # what the voice front end listens for
    spoken_style: str = ''                   # optional paragraph added to every spoken prompt (dialect, tone)
    capabilities: list = field(default_factory=list)   # lines for "WHAT THE HOUSE CAN DO"
    # --- models ----------------------------------------------------------------------
    screen_model: str = 'claude-haiku-4-5-20251001'
    recall_model: str = 'claude-haiku-4-5-20251001'
    nightly_model: str = 'claude-sonnet-5'
    screen_timeout_s: float = 8.0            # past this the caller's fallback line is the answer
    recall_timeout_s: float = 14.0
    recent_window_s: int = 300               # how much of the last minutes each screen call sees
    # --- memory ----------------------------------------------------------------------
    embed_threads: int = 2
    context_min_sim: float = 0.6             # below this cosine a "from before" hit is room noise
    idea_dup_sim: float = 0.86               # a new idea this close to any known idea is dropped
    fact_dup_sim: float = 0.85               # a proposed fact this close to a known/queued one is dropped
    question_dup_sim: float = 0.90
    fact_min_confidence: float = 0.6
    # --- speaking first (rare on purpose) ---------------------------------------------
    share_per_day: list = field(default_factory=lambda: [1, 3])   # drawn per calendar date
    share_gap_s: int = 7200                  # never two inside one conversation
    share_hours: list = field(default_factory=lambda: [9, 21])    # local hours it may volunteer
    share_after_talk_s: int = 600            # only while a conversation is warm
    question_prefix: str = "Something I've been wondering: "
    question_drop_days: int = 7              # asked and never answered: dropped after this long

    # ----------------------------------------------------------------------------------
    @classmethod
    def load(cls, path=None):
        """`path`, else $SOUL_CONFIG, else defaults. Unknown keys are an error: a typo must not be silent."""
        path = path or os.environ.get('SOUL_CONFIG')
        data = {}
        if path:
            with open(os.path.expanduser(path), 'rb') as f:
                data = tomllib.load(f)
        names = {f.name for f in dataclasses.fields(cls)}
        bad = sorted(set(data) - names)
        if bad:
            raise ValueError(f'unknown config key(s): {", ".join(bad)}')
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self):
        if len(self.share_per_day) != 2 or not 0 <= self.share_per_day[0] <= self.share_per_day[1]:
            raise ValueError('share_per_day must be [low, high] with 0 <= low <= high')
        if len(self.share_hours) != 2 or not 0 <= self.share_hours[0] < self.share_hours[1] <= 24:
            raise ValueError('share_hours must be [from, to] within 0..24, from < to')
        for k in ('context_min_sim', 'idea_dup_sim', 'fact_dup_sim', 'question_dup_sim', 'fact_min_confidence'):
            if not 0.0 <= getattr(self, k) <= 1.0:
                raise ValueError(f'{k} must be between 0 and 1')

    # derived paths ------------------------------------------------------------------
    @property
    def dir(self):
        return os.path.expanduser(self.data_dir)

    @property
    def models(self):
        return os.path.expanduser(self.model_dir) if self.model_dir else os.path.join(self.dir, 'models', 'bge-small-en-v1.5')

    def path(self, name):
        return os.path.join(self.dir, name)

    @property
    def db_path(self):
        return os.environ.get('SOUL_DB') or self.path('soul.db')
