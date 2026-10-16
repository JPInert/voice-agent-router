"""Runs only when the embedding model is installed (python3 -m soul get-model, or set SOUL_MODEL_DIR).
Skipped otherwise, so the offline suite never needs the download. These are the tests that show the
MEANING half of recall is real: a hashing embedder cannot know that a vet is an animal doctor."""
import os
import unittest

from soul import Config, embed
from soul.demo import materialise

from .helpers import SoulCase

MODEL_DIR = os.path.expanduser(os.environ.get('SOUL_MODEL_DIR') or Config().models)


@unittest.skipUnless(embed.installed(MODEL_DIR), f'embedding model not installed in {MODEL_DIR}')
class TestRealEmbedder(SoulCase):
    @classmethod
    def setUpClass(cls):
        cls.emb = embed.OnnxEmbedder(MODEL_DIR, threads=2)

    def test_vectors_are_normalised_384(self):
        v = self.emb.embed(['hello there', 'a second sentence'])
        self.assertEqual(v.shape, (2, 384))
        self.assertAlmostEqual(float((v[0] ** 2).sum()), 1.0, places=4)

    def test_paraphrase_scores_above_unrelated(self):
        v = self.emb.embed(['Sam keeps tomatoes on the balcony', 'Sam grows tomatoes out on the balcony',
                            'The train to the coast leaves at noon'])
        self.assertGreater(float(v[0] @ v[1]), float(v[0] @ v[2]) + 0.2)
        self.assertGreater(float(v[0] @ v[1]), 0.85)                                 # the fact-dedupe threshold

    def test_meaning_finds_what_keywords_cannot(self):
        s = self.make(embedder=embed.OnnxEmbedder(MODEL_DIR, threads=2))
        materialise(self.dir)
        q = 'when does the animal see the doctor'                                   # shares no word with "vet"
        vet = s.search(q, k=5)
        self.assertTrue(any('vet' in r['text'] for r in vet[:3]), [r['text'] for r in vet])

    def test_keyword_only_misses_the_same_questions(self):
        s = self.make(embedder=False)
        materialise(self.dir)
        vet = s.search('when does the animal see the doctor', k=5)
        self.assertFalse(any('vet' in r['text'] for r in vet))


if __name__ == '__main__':
    unittest.main()
