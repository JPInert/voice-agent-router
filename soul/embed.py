"""Local meaning vectors: bge-small-en-v1.5, quantised ONNX, CPU only, no network at run time.

The model is NOT in this repo. `python3 -m soul get-model` (or get_model.py in this package) downloads two files
from Hugging Face (see README). Anything with `.dim`, `.embed(texts)` and `.embed_query(text)` can
stand in for it; the tests use a hashing embedder.
"""
import os
import threading

QUERY_PREFIX = 'Represent this sentence for searching relevant passages: '   # bge's query side
MODEL_FILES = ('model_quantized.onnx', 'tokenizer.json')


def installed(model_dir):
    return all(os.path.exists(os.path.join(model_dir, f)) for f in MODEL_FILES)


class OnnxEmbedder:
    dim = 384

    def __init__(self, model_dir, threads=2):
        self.model_dir, self.threads = model_dir, threads
        self._lock = threading.Lock()
        self._rt = None

    def _load(self):
        with self._lock:
            if self._rt is None:
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
                tok = Tokenizer.from_file(os.path.join(self.model_dir, 'tokenizer.json'))
                tok.enable_truncation(256)
                tok.enable_padding()
                so = ort.SessionOptions()
                so.intra_op_num_threads = self.threads     # never take every core on a shared box
                so.inter_op_num_threads = 1
                sess = ort.InferenceSession(os.path.join(self.model_dir, 'model_quantized.onnx'), so,
                                            providers=['CPUExecutionProvider'])
                self._rt = (np, tok, sess)
        return self._rt

    def embed(self, texts):
        """(n, 384) float32, L2-normalised, so a dot product is a cosine."""
        np, tok, sess = self._load()
        out = []
        for i in range(0, len(texts), 32):
            enc = tok.encode_batch(list(texts[i:i + 32]))
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            v = sess.run(None, {'input_ids': ids, 'attention_mask': mask,
                                'token_type_ids': np.zeros_like(ids)})[0][:, 0]
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text):
        return self.embed([QUERY_PREFIX + text])[0]
