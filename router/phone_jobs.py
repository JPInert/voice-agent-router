"""The phone side of a miss: answer "On it." at once, run the worker in the background, hand the
sentence back when the phone asks for it.

The phone client posts the words it heard (`handle_phone`) and gets a reply within a second or two.
For a miss that is only {'what': 'claude', 'said': ['On it.'], 'pending': <id>}; the client then
posts that id to collect the worker's sentence (`PhoneJobs.collect`), which blocks up to ~80 s.

Two rules from production:

  * Owner only. `who` is asserted by the client, so it is advisory: the bearer token on the endpoint
    is the real boundary, and anyone holding it can say "owner". A request with no `who` is a guest.
    A guest's misses (`who != owner`) stay misses: the worker has the owner's mail and
    calendar, and another person's phone must never reach them.
  * `no_claude=True` for the worker's own `house` calls. Without it, a miss inside the worker would
    start another worker.
"""
import threading
import time
import uuid

TTL_S = 600


class PhoneJobs:
    def __init__(self, worker, ttl_s=TTL_S, log=None, clock=time.time):
        self.worker, self.ttl_s, self.clock = worker, ttl_s, clock
        self.log = log or (lambda *a: print(*a, flush=True))
        self.jobs = {}
        self._lock = threading.Lock()

    def _purge(self):
        now = self.clock()
        for k in [k for k, j in self.jobs.items() if now - j['ts'] > self.ttl_s]:
            del self.jobs[k]

    def start(self, text, away=False):
        jid = uuid.uuid4().hex[:12]
        job = {'done': threading.Event(), 'result': None, 'ts': self.clock()}
        with self._lock:
            self._purge()
            self.jobs[jid] = job

        def run():
            try:
                r = self.worker.run(text, away=away)
                job['result'] = r
                # the one line tools/measure_phone_jobs.py counts: id, outcome, seconds, tools. Never the words.
                self.log(f"phone claude {jid}: rc=0 {r['secs']}s tools={r['tools']}")
            except Exception as e:                     # class name only: the argv is the owner's words
                job['result'] = {'sentence': 'That took too long, sorry.', 'tools': [], 'secs': 0}
                self.log(f'phone claude {jid}: {e.__class__.__name__}')
            job['done'].set()

        threading.Thread(target=run, daemon=True).start()
        self.log(f'phone claude {jid}: in the background')
        return jid

    def collect(self, jid, wait_s=80):
        """The worker's sentence, or None for an unknown id; 'still working' when wait_s runs out."""
        job = self.jobs.get(jid)
        if job is None:
            return None
        if not job['done'].wait(wait_s):
            return {'sentence': 'Still working on it.', 'tools': [], 'pending': True}
        return job['result']


def handle_phone(text, who, reflex, jobs, owner='owner', no_claude=False, away=False):
    """One phone request. Tier 0 first (no model); a miss from the owner starts the worker."""
    text = ' '.join(str(text or '').split())
    if not text:
        return {'miss': True, 'why': 'nothing heard'}
    matched, said = reflex(text)
    if matched:
        return {'miss': False, 'what': 'trick', 'text': text, 'said': said}
    if who == owner and not no_claude:
        jid = jobs.start(text, away=away)
        return {'miss': False, 'what': 'claude', 'text': text, 'said': ['On it.'], 'pending': jid}
    return {'miss': True, 'why': 'no match', 'text': text}
