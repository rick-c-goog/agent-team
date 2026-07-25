"""Embeddings + chunking (DESIGN.md §4.1.2).

Embeddings are pluggable. The default ``HashingEmbedding`` is dependency-free and
deterministic — a hashed bag-of-words projected onto a fixed-dimension unit vector — so
the whole knowledge base, and its tests, run offline exactly like the mock runtime does
for the loop. Swap in a hosted embedding model for production recall by implementing
``EmbeddingProvider``; nothing else changes.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")

# ~4 chars/token is close enough for budgeting without a tokenizer dependency.
CHARS_PER_TOKEN = 4
DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP_RATIO = 0.15


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def approx_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


class EmbeddingProvider(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into unit vectors."""
        ...


class HashingEmbedding:
    """Deterministic offline embedding: hashed term frequencies, L2-normalized."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _bucket(self, term: str) -> int:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for term in tokenize(text):
                vec[self._bucket(term)] += 1.0
            # sublinear scaling damps repeated terms, then L2-normalize for cosine
            vec = [math.log1p(v) for v in vec]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def keyword_overlap(query_terms: set[str], text: str) -> float:
    """Lexical half of hybrid ranking — catches exact names an embedding may blur."""
    terms = set(tokenize(text))
    if not terms or not query_terms:
        return 0.0
    return len(terms & query_terms) / math.sqrt(len(terms))


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_segment(
    text: str,
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[str]:
    """Split one segment into overlapping chunks on paragraph/sentence boundaries.

    Callers that must not split (a CSV row) simply never exceed max_tokens, so this
    returns the row untouched.
    """
    if approx_tokens(text) <= max_tokens:
        return [text]

    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = int(max_chars * overlap_ratio)
    # Prefer paragraph boundaries, fall back to sentence, then hard cut.
    units = [u for u in re.split(r"(?<=\n)\n+", text) if u.strip()] or [text]
    if max(len(u) for u in units) > max_chars:
        units = [u for u in re.split(r"(?<=[.!?])\s+", text) if u.strip()] or [text]

    chunks: list[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + len(unit) + 1 > max_chars:
            chunks.append(cur.strip())
            tail = cur[-overlap_chars:] if overlap_chars else ""
            cur = (tail + "\n" + unit) if tail else unit
        else:
            cur = (cur + "\n" + unit) if cur else unit
        while len(cur) > max_chars:                 # a single oversized unit
            chunks.append(cur[:max_chars].strip())
            cur = cur[max_chars - overlap_chars:]
    if cur.strip():
        chunks.append(cur.strip())
    return chunks
