"""Per-agent knowledge base and RAG (DESIGN.md §4.1).

Memory is what an agent *learned*; knowledge is what it *was given to read*. Sources
(web URL, Google Drive folder, local/uploaded file) are ingested, chunked with a
citable locator, embedded, and retrieved during the Plan and Build nodes — with
citations the Tester can check.
"""

from .service import KnowledgeService
from .extractors import Segment, ExtractionError, extract
from .embeddings import HashingEmbedding, EmbeddingProvider

__all__ = [
    "KnowledgeService",
    "Segment",
    "ExtractionError",
    "extract",
    "HashingEmbedding",
    "EmbeddingProvider",
]
