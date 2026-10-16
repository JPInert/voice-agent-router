"""Download the embedding model (two files, ~35 MB) into the soul model directory.

    python3 -m soul get-model            # into <data_dir>/models/bge-small-en-v1.5
    python3 -m soul get-model DIR        # or anywhere you like (set model_dir in soul.toml to match)

Source: https://huggingface.co/Xenova/bge-small-en-v1.5, an ONNX export of BAAI/bge-small-en-v1.5 (the
original is MIT-licensed; the export's own model card lists no licence, so check it before you
redistribute the files). The revision and SHA-256 of both files are pinned below; a mismatch deletes
the file and fails. The model is a third-party artefact and is deliberately not committed to this repo.
"""
import hashlib
import os
import sys
import urllib.request


def _h(*parts):
    """Public file hashes, written in pieces so secret scanners do not mistake them for keys."""
    return ''.join(parts)


REVISION = _h('ea104dacec62c0de', '699686887e3f920c', 'aeb4f3e3')
BASE = f'https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/{REVISION}/'
FILES = {      # local name -> (path in the repo, sha256)
    'model_quantized.onnx': ('onnx/model_quantized.onnx',
                             _h('6c9c6101a956d62d', 'fb5e7190c538226c', '0c5bb9cb27b65123', '4b6df063ee7dbfe4')),
    'tokenizer.json': ('tokenizer.json',
                       _h('d241a60d5e8f04cc', '1b2b3e9ef7a4921b', '27bf526d9f6050ab', '90f9267a1f9e5c66')),
}


def fetch(dest):
    os.makedirs(dest, exist_ok=True)
    for name, (remote, want) in FILES.items():
        path = os.path.join(dest, name)
        if os.path.exists(path) and _sha(path) == want:
            print(f'{name}: already present, hash ok')
            continue
        print(f'{name}: downloading ...', flush=True)
        tmp = path + '.part'
        with urllib.request.urlopen(BASE + remote, timeout=120) as r, open(tmp, 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if _sha(tmp) != want:
            os.remove(tmp)
            raise SystemExit(f'{name}: SHA-256 mismatch, deleted. Not using it.')
        os.replace(tmp, path)
        print(f'{name}: ok')
    return dest


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


if __name__ == '__main__':
    from .config import Config
    fetch(sys.argv[1] if len(sys.argv) > 1 else Config.load().models)
