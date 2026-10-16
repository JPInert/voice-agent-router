"""Small text helpers shared by every module: the one word rule, JSON-out-of-a-reply, and the guard
that keeps code and paths out of a sentence that will be spoken aloud."""
import json
import re

STOPW = set("""a an the to and or of in on at is it its i im me my we our you your that this so but just
like do does did be was were are am not what why how for with all up there here then now about
remember please yesterday today last night when her his she him he they them doing going
get got one some thing things really okay yeah know want need can could would should will has have had
been being any much many""".split())


def words(t):
    """Content words of `t`: lowercase alphanumerics, no stop words, longer than 2 characters.
    The ONE rule used by forget-matching, keyword search and word-overlap fallbacks."""
    return [w for w in re.findall(r"[a-z0-9]+", (t or '').lower()) if w not in STOPW and len(w) > 2]


def norm(t):
    return ' '.join((t or '').split())


def json_from(text):
    """The first JSON object in a model reply (code fences tolerated), or None."""
    if not text:
        return None
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.S).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    i = t.find('{')
    if i >= 0:
        try:
            v = json.JSONDecoder().raw_decode(t[i:])[0]
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None


CODE_CHARS = set('{}[]<>|\\$=*#~^`;')


def speakable(sent):
    """A reply is read aloud, so paths, flags, file names and code have no place in it. Rewrite the
    speakable bits, drop the rest; a sentence that is still mostly symbols goes unsaid ('')."""
    t = re.sub(r'```.*?```', ' ', sent or '', flags=re.S)
    t = re.sub(r'`([^`]*)`', lambda m: m.group(1) if re.fullmatch(r"[\w .'-]{1,24}", m.group(1)) else ' ', t)
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'(?:~|\.{1,2})?(?:/[\w.@-]+){2,}/?', '', t)
    t = re.sub(r'\b([\w-]+)\.(?:py|sh|js|ts|json|ya?ml|md|txt|log|conf|toml|html|css|service)\b',
               lambda m: m.group(1).replace('_', ' ').replace('-', ' '), t)
    t = re.sub(r'(?<!\w)--?([a-zA-Z][\w-]*)', lambda m: m.group(1).replace('-', ' '), t)
    t = re.sub(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b', lambda m: m.group(0).replace('_', ' ').lower(), t)
    t = re.sub(r'\s+([,.;:!?])', r'\1', re.sub(r'\s{2,}', ' ', t)).strip()
    sym = sum(c in CODE_CHARS for c in t)
    if not re.search(r'[A-Za-z]{2,}', t) or sym > max(3, len(t) * 0.08):
        return ''
    return t


def slurp(path, **kw):
    """Whole text file, closed again. Raises OSError like open()."""
    with open(path, **kw) as f:
        return f.read()
