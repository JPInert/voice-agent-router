"""The long-term memory index: one SQLite file over the plain files in the data directory.

The files (events-DATE.jsonl, thoughts.md, ideas.md, memory.jsonl, diary/) stay the source of truth
and the backup. Every writer keeps appending to them; `Store.sync()` INGESTS them into soul.db by byte
offset, so the hot path never changes and nothing is lost if soul.db is deleted. What the database
adds that files cannot:

    docs       every heard/said/routed/miss line, thought, idea, diary day and saved fact
    docs_fts   BM25 keyword index over docs.text (FTS5)
    vecs       a meaning vector per doc (embedder.embed), cached in-process
    ideas      each idea's state: open | done | dropped
    pending    facts the nightly INFERRED about the person, waiting for their yes (never saved before it)
    shares     lines it would like to volunteer, and questions it wonders about (rare-speak-first)
    spoke      every unprompted line it actually said: the ledger that keeps it rare

`search()` fuses the keyword rank and the meaning rank (reciprocal rank fusion), so a paraphrase finds
the line and an exact name still wins. With no embedder it degrades to keyword-only.

State that exists only in the database (idea states, pending decisions, shares, what was spoken) is
mirrored to ledger.jsonl and replayed into a fresh soul.db, so deleting the database is safe.
"""
import contextlib
import glob
import json
import os
import re
import sqlite3
import time

from .text import norm, slurp, words

EVENT_KINDS = ('heard', 'said', 'routed', 'miss')
NOTE_RE = re.compile(r'^- (\d\d)-(\d\d) (\d\d):(\d\d) \(([^)]*)\) (.+)$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ts REAL NOT NULL,
    day TEXT NOT NULL, text TEXT NOT NULL, meta TEXT, UNIQUE(kind, ts, text));
CREATE INDEX IF NOT EXISTS docs_day ON docs(day);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(text, content='docs', content_rowid='id',
    tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF text ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
    DELETE FROM vecs WHERE doc_id = new.id; END;
CREATE TABLE IF NOT EXISTS vecs (doc_id INTEGER PRIMARY KEY, v BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS vec_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS vecs_ai AFTER INSERT ON vecs BEGIN
    INSERT INTO vec_log(doc_id) VALUES (new.doc_id); END;
CREATE TABLE IF NOT EXISTS ingest (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, ino INTEGER);
CREATE TABLE IF NOT EXISTS ideas (doc_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'open',
    updated REAL, note TEXT);
CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY, ts REAL NOT NULL, fact TEXT NOT NULL,
    confidence REAL, evidence TEXT, day TEXT, state TEXT NOT NULL DEFAULT 'pending', decided REAL);
CREATE TABLE IF NOT EXISTS spoke (id INTEGER PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shares (id INTEGER PRIMARY KEY, day TEXT NOT NULL, text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting', ts REAL, kind TEXT NOT NULL DEFAULT 'share', answer TEXT);
"""


def _day(ts):
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def _note_ts(mo, d, h, mi, now=None):
    """thoughts.md / ideas.md stamp lines with MM-DD only; a month ahead of now is last year."""
    now = time.localtime(now or time.time())
    y = now.tm_year - (1 if int(mo) > now.tm_mon else 0)
    return time.mktime((y, int(mo), int(d), int(h), int(mi), 0, 0, 0, -1))


def _live_memories(lines):
    """memory.jsonl -> (live, forgotten): a `forget` removes the matching facts said BEFORE it, so a
    fact re-remembered later stays."""
    live, gone = [], []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('fact'):
            live.append(r)
        elif r.get('forget'):
            ws = set(words(r['forget']))
            hit = [m for m in live if ws and ws <= set(words(m['fact']))]
            gone += hit
            live = [m for m in live if m not in hit]
    return live, gone


class Store:
    def __init__(self, cfg, embedder=None, log=None):
        self.cfg, self.embedder = cfg, embedder
        self.log = log or (lambda *a: None)
        self._vec = None                      # (vec_log high-water, ids, matrix), read once per process

    # --- connection -----------------------------------------------------------------
    @property
    def src(self):
        return self.cfg.dir

    def connect(self):
        os.makedirs(os.path.dirname(self.cfg.db_path) or '.', exist_ok=True)
        c = sqlite3.connect(self.cfg.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=10000')          # several writers: the voice loop, the nightly, the CLI
        c.executescript(SCHEMA)
        return c

    @contextlib.contextmanager
    def _c(self, c=None):
        if c is not None:
            yield c
            return
        c = self.connect()
        try:
            yield c
        finally:
            c.close()

    def session(self):
        """`with store.session() as c:` one connection for several calls, closed afterwards."""
        return self._c()

    # --- ledger: DB-only state mirrored to a file --------------------------------------
    def _ledger(self, op, **kw):
        try:
            os.makedirs(self.src, exist_ok=True)
            with open(os.path.join(self.src, 'ledger.jsonl'), 'a') as f:
                f.write(json.dumps(dict(kw, op=op, at=round(time.time(), 3))) + '\n')
        except OSError:
            pass

    def replay_ledger(self, c):
        """Called once by sync() on a fresh/rebuilt soul.db. Keys are texts, not row ids."""
        path = os.path.join(self.src, 'ledger.jsonl')
        if not os.path.exists(path):
            return 0
        n = 0
        with c:
            for line in slurp(path, errors='replace').splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                op = r.get('op')
                if op == 'idea':
                    c.execute("UPDATE ideas SET state=?, note=?, updated=? WHERE doc_id IN "
                              "(SELECT id FROM docs WHERE kind='idea' AND text=?)",
                              (r['state'], r.get('note'), r['at'], r['text']))
                elif op == 'propose':
                    c.execute('INSERT INTO pending(ts, fact, confidence, evidence, day) VALUES (?,?,?,?,?)',
                              (r['at'], r['fact'], r.get('confidence'), r.get('evidence'), r.get('day')))
                elif op == 'decide':
                    c.execute("UPDATE pending SET state=?, decided=? WHERE fact=? AND state='pending'",
                              (r['state'], r['at'], r['fact']))
                elif op == 'share':
                    if not r.get('keep'):
                        c.execute("UPDATE shares SET state='stale' WHERE state='waiting' AND kind='share'")
                    c.execute('INSERT INTO shares(day, text, kind) VALUES (?,?,?)',
                              (r['day'], r['text'], r.get('kind') or 'share'))
                elif op == 'spoke':
                    c.execute('INSERT INTO spoke(ts, text) VALUES (?,?)', (r['at'], r['text']))
                    if r.get('share_text'):
                        c.execute("UPDATE shares SET state=CASE kind WHEN 'question' THEN 'asked' ELSE 'said' END, ts=? "
                                  "WHERE text=? AND state='waiting'", (r['at'], r['share_text']))
                elif op == 'question':
                    c.execute("UPDATE shares SET state=?, answer=? WHERE kind='question' AND text=?",
                              (r['state'], r.get('answer'), r['text']))
                n += 1
        return n

    # --- ingest ---------------------------------------------------------------------
    def _add(self, c, kind, ts, text, meta=None):
        text = norm(text)
        if not text:
            return None
        cur = c.execute('INSERT OR IGNORE INTO docs(kind, ts, day, text, meta) VALUES (?,?,?,?,?)',
                        (kind, ts, _day(ts), text, json.dumps(meta) if meta else None))
        return cur.lastrowid if cur.rowcount else None

    def _new_lines(self, c, path):
        """(lines appended to `path` since the last sync, reset). reset=True means the file was
        replaced, shrunk or edited in place and these are ALL its lines: the caller reconciles."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return [], False
        row = c.execute('SELECT offset, ino FROM ingest WHERE path=?', (path,)).fetchone()
        off = row['offset'] if row and row['ino'] == st.st_ino and row['offset'] <= st.st_size else 0
        if off:
            with open(path, 'rb') as f:              # an in-place edit moves line starts: offset mid-line
                f.seek(off - 1)
                if f.read(1) != b'\n':
                    off = 0
        reset = bool(row) and row['offset'] > 0 and off == 0
        if off == st.st_size:
            return [], False
        with open(path, 'rb') as f:
            f.seek(off)
            data = f.read()
        end = data.rfind(b'\n') + 1          # a half-written last line waits for the next sync
        c.execute('INSERT OR REPLACE INTO ingest(path, offset, ino) VALUES (?,?,?)', (path, off + end, st.st_ino))
        return data[:end].decode('utf-8', 'replace').splitlines(), reset

    def _drop_docs(self, c, ids):
        for i in ids:
            c.execute('DELETE FROM vecs WHERE doc_id=?', (i,))
            c.execute('DELETE FROM ideas WHERE doc_id=?', (i,))
            c.execute('DELETE FROM docs WHERE id=?', (i,))

    def sync(self, c=None):
        """Pull everything new from the files into the DB. Idempotent; cheap when nothing changed."""
        with self._c(c) as c:
            return self._sync(c)

    def _sync(self, c):
        n = 0
        d = self.src
        fresh = not c.execute('SELECT 1 FROM ingest LIMIT 1').fetchone()
        with c:
            for path in sorted(glob.glob(os.path.join(d, 'events-*.jsonl'))):
                for line in self._new_lines(c, path)[0]:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get('kind') in EVENT_KINDS and e.get('text'):
                        meta = {k: e[k] for k in ('what', 'why', 'source') if e.get(k)}
                        n += bool(self._add(c, e['kind'], e.get('ts') or 0, e['text'], meta))
            for kind, fname in (('thought', 'thoughts.md'), ('idea', 'ideas.md')):
                lines, reset = self._new_lines(c, os.path.join(d, fname))
                seen = set()
                for line in lines:
                    m = NOTE_RE.match(line.strip())
                    if not m:
                        continue
                    ts = _note_ts(*m.groups()[:4])
                    seen.add((ts, norm(m.group(6))))
                    rid = self._add(c, kind, ts, m.group(6), {'source': m.group(5)})
                    if rid and kind == 'idea':
                        c.execute('INSERT OR IGNORE INTO ideas(doc_id, state, updated) VALUES (?,?,?)', (rid, 'open', ts))
                    n += bool(rid)
                if reset:                      # a line deleted or edited by hand is gone from the index too
                    self._drop_docs(c, [r['id'] for r in c.execute('SELECT id, ts, text FROM docs WHERE kind=?', (kind,))
                                        if (r['ts'], r['text']) not in seen])
            mpath = os.path.join(d, 'memory.jsonl')
            if self._new_lines(c, mpath)[0]:        # tiny file: any change rebuilds the memory docs from it
                live, gone = _live_memories(slurp(mpath, errors='replace').splitlines())
                want = {('memory', m['ts'], norm(m['fact'])) for m in live} | \
                       {('forgotten', m['ts'], norm(m['fact'])) for m in gone}
                have = {(r['kind'], r['ts'], r['text']): r['id'] for r in
                        c.execute("SELECT id, kind, ts, text FROM docs WHERE kind IN ('memory','forgotten')")}
                self._drop_docs(c, [i for k, i in have.items() if k not in want])
                for kind, ts, text in want - set(have):
                    n += bool(self._add(c, kind, ts, text))
            for path in sorted(glob.glob(os.path.join(d, 'diary', '*.md'))):
                day = os.path.basename(path)[:-3]
                try:
                    body = norm(' '.join(l for l in slurp(path).splitlines() if not l.startswith('# ')))
                    ts = time.mktime(time.strptime(day, '%Y-%m-%d')) + 86399
                except (OSError, ValueError):
                    continue
                row = c.execute("SELECT id, text FROM docs WHERE kind='diary' AND day=?", (day,)).fetchone()
                if not row:
                    n += bool(self._add(c, 'diary', ts, body))
                elif row['text'] != body:
                    c.execute('UPDATE docs SET text=? WHERE id=?', (body, row['id']))
        if fresh:
            n += self.replay_ledger(c)
        return n

    # --- meaning vectors ---------------------------------------------------------------
    @property
    def dim(self):
        return getattr(self.embedder, 'dim', 0)

    def embed_missing(self, c=None, limit=5000):
        """Embed docs that have no vector yet. 0 (and no error) when there is no embedder."""
        if self.embedder is None:
            return 0
        with self._c(c) as c:
            rows = c.execute('SELECT id, text FROM docs WHERE id NOT IN (SELECT doc_id FROM vecs) '
                             "AND kind != 'forgotten' LIMIT ?", (limit,)).fetchall()
            if not rows:
                return 0
            V = self.embedder.embed([r['text'][:1000] for r in rows])
            with c:
                c.executemany('INSERT OR REPLACE INTO vecs(doc_id, v) VALUES (?,?)',
                              [(r['id'], V[i].tobytes()) for i, r in enumerate(rows)])
            return len(rows)

    def _vec_matrix(self, c):
        """Every vector, cached in-process and topped up from vec_log (each vecs insert, including a
        re-embed of the same doc, logs a new seq). A full read per query grows with the corpus."""
        import numpy as np
        hw = c.execute('SELECT COALESCE(MAX(seq), 0) FROM vec_log').fetchone()[0]
        if self._vec is None:
            rows = c.execute('SELECT doc_id, v FROM vecs').fetchall()
            ids = np.array([r['doc_id'] for r in rows], dtype=np.int64)
            M = (np.frombuffer(b''.join(r['v'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                 if rows else np.zeros((0, self.dim), np.float32))
            self._vec = (hw, ids, M)
            return ids, M
        old_hw, ids, M = self._vec
        if hw > old_hw:
            rows = c.execute('SELECT doc_id, v FROM vecs WHERE doc_id IN (SELECT doc_id FROM vec_log WHERE seq > ?)',
                             (old_hw,)).fetchall()
            if rows:
                new_ids = np.array([r['doc_id'] for r in rows], dtype=np.int64)
                new_M = np.frombuffer(b''.join(r['v'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                keep = ~np.isin(ids, new_ids)
                ids, M = np.concatenate([ids[keep], new_ids]), np.concatenate([M[keep], new_M])
            self._vec = (hw, ids, M)
        return ids, M

    @staticmethod
    def _fts_query(q):
        ws = words(q)
        return ' OR '.join(f'"{w}"*' for w in ws[:12])

    def search(self, q, k=20, kinds=None, since=None, until=None, min_sim=0.0, c=None):
        """Keyword + meaning, fused. kinds: e.g. ('heard','diary','thought','memory'). Returns rows
        (id, kind, ts, day, text, score, sim) best first. Keyword-only when there is no embedder."""
        import numpy as np
        with self._c(c) as c:
            self._sync(c)
            try:
                self.embed_missing(c, limit=200)     # hot path: a bounded batch; `db`/the nightly embed the rest
            except Exception as e:
                self.log('embed failed, keyword-only:', e.__class__.__name__, e)
            where, args = ["d.kind != 'forgotten'"], []
            if kinds:
                where.append('d.kind IN (%s)' % ','.join('?' * len(kinds)))
                args += list(kinds)
            if since:
                where.append('d.ts >= ?')
                args.append(since)
            if until:
                where.append('d.ts < ?')
                args.append(until)
            W = ' AND '.join(where)
            ranks, sims, fts_rank = {}, {}, {}
            fq = self._fts_query(q)
            if fq:
                for i, r in enumerate(c.execute(f'SELECT d.id FROM docs_fts f JOIN docs d ON d.id=f.rowid '
                                                f'WHERE docs_fts MATCH ? AND {W} ORDER BY bm25(docs_fts) LIMIT 60',
                                                [fq] + args)):
                    ranks[r['id']] = ranks.get(r['id'], 0.0) + 1.0 / (60 + i)
                    fts_rank[r['id']] = i
            if self.embedder is not None:
                try:
                    allowed = [r[0] for r in c.execute(f'SELECT d.id FROM docs d WHERE {W}', args)]
                    ids, M = self._vec_matrix(c)
                    if len(ids) and allowed:
                        keep = np.isin(ids, np.array(allowed, dtype=np.int64))
                        ids_k, M_k = ids[keep], M[keep]
                        s = M_k @ self.embedder.embed_query(q)
                        sims = {int(i): float(x) for i, x in zip(ids_k, s)}
                        for i, j in enumerate(np.argsort(-s)[:60]):
                            if s[j] < 0.45:          # bge-small: below this is noise, not meaning
                                break
                            ranks[int(ids_k[j])] = ranks.get(int(ids_k[j]), 0.0) + 1.0 / (60 + i)
                except Exception as e:
                    self.log('vector search failed, keyword-only:', e.__class__.__name__, e)
            top = sorted(ranks.items(), key=lambda x: -x[1])[:k * 4]
            out = []
            if top:
                got = {r['id']: r for r in c.execute('SELECT id, kind, ts, day, text FROM docs WHERE id IN (%s)'
                                                     % ','.join('?' * len(top)), [i for i, _ in top])}
                seen = {}                    # "weather tomorrow" asked 9 times is one memory: keep the latest
                for i, s in top:
                    if i not in got:
                        continue
                    key = got[i]['text'].lower().strip(' .!?')
                    if key in seen:
                        if got[i]['ts'] > seen[key]['ts']:
                            seen[key].update(dict(got[i]))
                        continue
                    sim = sims.get(i, 0.0)
                    if min_sim and sim < min_sim and fts_rank.get(i, 99) >= 5:
                        continue             # weak meaning AND not a top keyword hit: noise
                    seen[key] = dict(got[i], score=round(s, 4), sim=round(sim, 3))
                    out.append(seen[key])
                out = out[:k]
            return out

    def related_to(self, texts, k=10, before=None, kinds=('diary', 'thought', 'memory', 'idea'), c=None):
        """Older docs that mean the same as any of `texts`: what the nightly reflects against."""
        import numpy as np
        texts = [t for t in texts if t]
        if not texts or self.embedder is None:
            return []
        with self._c(c) as c:
            self._sync(c)
            self.embed_missing(c)
            rows = c.execute('SELECT d.id, d.kind, d.day, d.text, v.v FROM vecs v JOIN docs d ON d.id=v.doc_id '
                             'WHERE d.kind IN (%s) AND d.ts < ?' % ','.join('?' * len(kinds)),
                             list(kinds) + [before or time.time()]).fetchall()
            if not rows:
                return []
            M = np.frombuffer(b''.join(r['v'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            Q = np.stack([self.embedder.embed_query(t) for t in texts])
            best = (M @ Q.T).max(axis=1)
            return [dict(id=rows[j]['id'], kind=rows[j]['kind'], day=rows[j]['day'], text=rows[j]['text'],
                         score=round(float(best[j]), 3)) for j in np.argsort(-best)[:k] if best[j] >= 0.6]

    # --- ideas: open until something closes them -----------------------------------------
    def open_ideas(self, limit=40, c=None):
        with self._c(c) as c:
            self._sync(c)
            return [dict(r) for r in c.execute(
                "SELECT d.id, d.day, d.text FROM ideas i JOIN docs d ON d.id=i.doc_id "
                "WHERE i.state='open' ORDER BY d.ts DESC LIMIT ?", (limit,))]

    def set_idea(self, doc_id, state, note='', c=None):
        with self._c(c) as c:
            with c:
                n = c.execute('UPDATE ideas SET state=?, updated=?, note=? WHERE doc_id=?',
                              (state, time.time(), note, doc_id)).rowcount
            if n:
                t = c.execute('SELECT text FROM docs WHERE id=?', (doc_id,)).fetchone()
                self._ledger('idea', text=t['text'] if t else '', state=state, note=note)
            return n

    def similar_idea(self, text, c=None):
        """An existing idea (any state) that says the same thing, so the nightly stops re-proposing it."""
        import numpy as np
        if self.embedder is None:
            return None
        with self._c(c) as c:
            self._sync(c)
            self.embed_missing(c)
            rows = c.execute('SELECT d.id, d.text, i.state, v.v FROM ideas i JOIN docs d ON d.id=i.doc_id '
                             'JOIN vecs v ON v.doc_id=d.id').fetchall()
            if not rows:
                return None
            M = np.frombuffer(b''.join(r['v'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            s = M @ self.embedder.embed([text])[0]
            j = int(np.argmax(s))
            if s[j] >= self.cfg.idea_dup_sim:
                return dict(id=rows[j]['id'], text=rows[j]['text'], state=rows[j]['state'], score=round(float(s[j]), 3))
            return None

    # --- facts it inferred, waiting for the person's yes ---------------------------------------
    def _profile_lines(self):
        try:
            return [l for l in slurp(os.path.join(self.src, 'profile.md')).splitlines() if l.startswith('- ')]
        except OSError:
            return []

    def propose(self, fact, confidence=None, evidence='', day=None, c=None):
        """Queue an inferred fact. Returns its pending id, or None when it is empty, already known,
        queued or rejected, or a paraphrase of one of those (cosine >= cfg.fact_dup_sim)."""
        fact = norm(fact).strip(' .')
        if not fact:
            return None
        with self._c(c) as c:
            known = [r['fact'] for r in c.execute('SELECT fact FROM pending')] + \
                    [r['text'] for r in c.execute("SELECT text FROM docs WHERE kind IN ('memory','forgotten')")]
            if fact.lower() in (k.lower() for k in known):
                return None
            known += [l.strip('- ').strip() for l in self._profile_lines()]
            if known and self.embedder is not None:
                try:
                    V = self.embedder.embed(known + [fact])
                    if float((V[:-1] @ V[-1]).max()) >= self.cfg.fact_dup_sim:
                        return None
                except Exception as e:
                    self.log('fact dedupe failed:', e.__class__.__name__)
            with c:
                rid = c.execute('INSERT INTO pending(ts, fact, confidence, evidence, day) VALUES (?,?,?,?,?)',
                                (time.time(), fact, confidence, evidence, day)).lastrowid
            self._ledger('propose', fact=fact, confidence=confidence, evidence=evidence, day=day)
            return rid

    def pending(self, state='pending', c=None):
        with self._c(c) as c:
            return [dict(r) for r in c.execute('SELECT * FROM pending WHERE state=? ORDER BY id', (state,))]

    def decide(self, pid, ok, c=None):
        """Mark a pending fact approved/rejected. The caller (Soul.approve) writes an approved one to
        memory.jsonl, which stays the truth for what it remembers. None if not pending."""
        with self._c(c) as c:
            with c:
                row = c.execute("SELECT * FROM pending WHERE id=? AND state='pending'", (pid,)).fetchone()
                if row:
                    c.execute('UPDATE pending SET state=?, decided=? WHERE id=?',
                              ('approved' if ok else 'rejected', time.time(), pid))
            if row:
                self._ledger('decide', fact=row['fact'], state='approved' if ok else 'rejected')
            return dict(row) if row else None

    # --- speaking first: the ledger that keeps it rare --------------------------------------
    def spoke_since(self, ts, c=None):
        with self._c(c) as c:
            return c.execute('SELECT COUNT(*) FROM spoke WHERE ts >= ?', (ts,)).fetchone()[0]

    def note_spoke(self, text, share_id=None, c=None):
        with self._c(c) as c:
            now = time.time()
            with c:
                c.execute('INSERT INTO spoke(ts, text) VALUES (?,?)', (now, text))
                if share_id:                     # a question waits for an answer: 'asked', not 'said'
                    c.execute("UPDATE shares SET state=CASE kind WHEN 'question' THEN 'asked' ELSE 'said' END, ts=? "
                              "WHERE id=?", (now, share_id))
            st = c.execute('SELECT text FROM shares WHERE id=?', (share_id,)).fetchone() if share_id else None
            self._ledger('spoke', text=text, share_text=st['text'] if st else None)

    def add_share(self, day, text, keep=False, kind='share', c=None):
        """A thing the nightly would like to tell the person; older unsaid ones go stale unless
        keep=True (the 2nd/3rd of one night's shares). kind='question': something it wonders about
        them. Questions never go stale and never stale anything; one already asked, waiting or
        answered is not added again."""
        text = norm(text)
        with self._c(c) as c:
            if text and kind == 'question' and self._question_known(c, text):
                text = ''
            if not text:
                return None
            with c:
                if not keep and kind == 'share':
                    c.execute("UPDATE shares SET state='stale' WHERE state='waiting' AND kind='share'")
                rid = c.execute('INSERT INTO shares(day, text, kind) VALUES (?,?,?)', (day, text, kind)).lastrowid
            self._ledger('share', day=day, text=text, keep=keep, kind=kind)
            return rid

    def waiting_share(self, now=None, c=None):
        """The next line to volunteer. Shares and questions take turns (at most one question a day);
        either one alone when only one kind is waiting. A share is only good for two days."""
        now = now or time.time()
        oldest = time.strftime('%Y-%m-%d', time.localtime(now - 2 * 86400))
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        with self._c(c) as c:
            sh = c.execute("SELECT * FROM shares WHERE state='waiting' AND kind='share' AND day >= ? "
                           "ORDER BY id DESC LIMIT 1", (oldest,)).fetchone()
            q = None
            if not c.execute("SELECT 1 FROM shares WHERE kind='question' AND state!='waiting' AND ts >= ?",
                             (midnight,)).fetchone():
                q = c.execute("SELECT * FROM shares WHERE state='waiting' AND kind='question' ORDER BY id LIMIT 1").fetchone()
            last = c.execute("SELECT kind FROM shares WHERE state!='waiting' AND ts IS NOT NULL "
                             "ORDER BY ts DESC LIMIT 1").fetchone()
            if q and sh and not (last and last['kind'] == 'share'):
                q = None                     # share and question take turns, so a 1-a-day quota still asks
            r = q or sh
            return dict(r) if r else None

    def _question_known(self, c, text):
        """Already waiting/asked/answered, as is or a near-verbatim rewording (cosine >= question_dup_sim).
        Meaning-level dedupe beyond that is the nightly's job: it sees its own question list."""
        qs = [r['text'] for r in c.execute("SELECT text FROM shares WHERE kind='question' "
                                           "AND state IN ('waiting','asked','answered')")]
        if text.lower() in (q.lower() for q in qs):
            return True
        if qs and self.embedder is not None:
            try:
                return float((self.embedder.embed(qs) @ self.embedder.embed([text])[0]).max()) >= self.cfg.question_dup_sim
            except Exception as e:
                self.log('question dedupe failed:', e.__class__.__name__)
        return False

    def questions(self, states=('waiting', 'asked', 'answered', 'dropped'), c=None):
        with self._c(c) as c:
            return [dict(r) for r in c.execute("SELECT * FROM shares WHERE kind='question' AND state IN (%s) ORDER BY id"
                                               % ','.join('?' * len(states)), states)]

    def set_question(self, qid, state, answer='', c=None):
        """The nightly read the person's reply (or saw it is no longer worth asking): answered|dropped."""
        with self._c(c) as c:
            with c:
                n = c.execute("UPDATE shares SET state=?, answer=? WHERE id=? AND kind='question' "
                              "AND state IN ('waiting','asked')", (state, answer, qid)).rowcount
            r = c.execute('SELECT text FROM shares WHERE id=?', (qid,)).fetchone()
            if n and r:
                self._ledger('question', text=r['text'], state=state, answer=answer)
            return n

    def stats(self, c=None):
        with self._c(c) as c:
            out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM docs GROUP BY kind')}
            out['vectors'] = c.execute('SELECT COUNT(*) FROM vecs').fetchone()[0]
            out['ideas'] = {r['state']: r['n'] for r in c.execute('SELECT state, COUNT(*) n FROM ideas GROUP BY state')}
            out['pending'] = {r['state']: r['n'] for r in c.execute('SELECT state, COUNT(*) n FROM pending GROUP BY state')}
            return out
