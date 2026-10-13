"""
Phase 2/3 — embedding abstraction.

One class per embedding backend, same interface (`embed(texts) -> vectors` for indexing,
`embed_query(text) -> vector` for retrieval), so the rest of the pipeline never cares which
one is active.

IMPORTANT — read this if you're running in a network-restricted sandbox:
`SentenceTransformerEmbedder` downloads model weights from Hugging Face Hub on first use.
If that network path is blocked in your environment, use `FakeEmbedder` to test the rest of
the pipeline (chunking -> Qdrant payload structure -> filtering) end-to-end, then switch back
to `SentenceTransformerEmbedder` on a machine with normal internet access. The two are
drop-in-compatible — only the constructor call needs to change.
"""
import hashlib


class SentenceTransformerEmbedder:
    """Production embedder. Requires: pip install sentence-transformers"""

    # BGE models are trained asymmetrically: passages are indexed as-is, but queries need this
    # instruction prefix prepended at *search* time to get good retrieval quality. Using the same
    # code path for both without this is a common, silent quality bug.
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Indexing-time embedding — no prefix. Used by ingestion/build_qdrant_index.py."""
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()

    def embed_query(self, text: str) -> list[float]:
        """Query-time embedding — BGE instruction prefix included. Used by retrieval at search time."""
        return self.model.encode(
            self.QUERY_PREFIX + text, normalize_embeddings=True, show_progress_bar=False
        ).tolist()


class FakeEmbedder:
    """
    Deterministic, offline, dependency-free stand-in — NOT semantically meaningful.
    Same text always produces the same vector, so equality/filter tests are still valid,
    but similarity search results will be meaningless. Use only to test plumbing.
    """

    def __init__(self, dim: int = 384):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._hash_vector(text)

    def _hash_vector(self, text: str) -> list[float]:
        # Build floats from plain integers (never reinterpret raw bytes as IEEE-754 —
        # arbitrary bit patterns can decode to NaN/Inf and poison the whole collection).
        vec = []
        h = hashlib.sha256(text.encode("utf-8")).digest()
        while len(vec) < self.dim:
            h = hashlib.sha256(h).digest()
            for i in range(0, len(h), 4):
                int_val = int.from_bytes(h[i : i + 4], "little")
                vec.append((int_val / 0xFFFFFFFF) * 2 - 1)  # safe range: [-1, 1]
        vec = vec[: self.dim]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]
