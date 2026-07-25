"""Knowledge Service — ingest, incremental sync, and retrieval (DESIGN.md §4.1).

Pipeline:  fetch → extract (citable segments) → chunk → embed → index
Retrieval: hybrid (vector cosine + keyword overlap), agent-scoped sources ranked ahead
of `scope: team` sources, returning ``Passage`` objects that carry their citation.

Sync is incremental by content hash: an unchanged document is skipped entirely, a
changed one is re-chunked and re-embedded, and one that disappeared from the source is
tombstoned rather than silently retained.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Optional

from ..models import Passage
from .embeddings import (
    EmbeddingProvider,
    HashingEmbedding,
    approx_tokens,
    chunk_segment,
    cosine,
    keyword_overlap,
    tokenize,
)
from .extractors import ExtractionError, extract
from .fetchers import FetchError, FetcherRegistry

# Ranking weights for hybrid retrieval; keyword overlap is what catches exact names
# (product tiers, personas) that a small hashed embedding can blur.
W_VECTOR = 0.65
W_KEYWORD = 0.35
TEAM_SCOPE_PENALTY = 0.95      # prefer the agent's own sources on a tie


@dataclass
class SyncReport:
    source_id: str
    ok: bool
    docs_indexed: int = 0
    docs_skipped: int = 0
    docs_tombstoned: int = 0
    chunks: int = 0
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"❌ {self.source_id}: {self.error}"
        return (f"✅ {self.source_id}: {self.docs_indexed} indexed, "
                f"{self.docs_skipped} unchanged, {self.docs_tombstoned} removed, "
                f"{self.chunks} chunks")


class KnowledgeService:
    def __init__(
        self,
        storage,
        embedding: Optional[EmbeddingProvider] = None,
        fetchers: Optional[FetcherRegistry] = None,
        knowledge_root: str = ".",
    ):
        self.storage = storage
        self.embedding = embedding or HashingEmbedding()
        self.fetchers = fetchers or FetcherRegistry(knowledge_root=knowledge_root)

    # ------------------------------------------------------------------ #
    # Source registry
    # ------------------------------------------------------------------ #
    def add_source(
        self,
        agent: Optional[str],
        type_: str,
        uri: str,
        *,
        scope: str = "agent",
        options: Optional[dict] = None,
        refresh_cron: Optional[str] = None,
        created_by: str = "system",
    ) -> str:
        """Register a source. Idempotent per (agent, uri) so re-running setup is safe."""
        owner = None if scope == "team" else agent
        existing = self.storage.find_source(owner, uri)
        if existing:
            return existing["id"]
        source_id = "src_" + uuid.uuid4().hex[:10]
        self.storage.add_source(
            source_id, owner, scope, type_, uri,
            json.dumps(options or {}), refresh_cron, created_by,
        )
        return source_id

    def remove_source(self, source_id: str) -> None:
        self.storage.remove_source(source_id)

    def sources_for(self, agent: str) -> list:
        return self.storage.sources_for(agent)

    def health(self) -> list[dict]:
        """What `/kb list` and the Mini App render (§4.1.4)."""
        out = []
        for row in self.storage.list_sources():
            out.append({
                "id": row["id"],
                "agent": row["agent_name"] or "(team)",
                "type": row["type"],
                "uri": row["uri"],
                "status": row["status"],
                "error": row["last_error"] or "",
                "docs": len(self.storage.docs_for_source(row["id"])),
                "chunks": self.storage.count_chunks(row["id"]),
                "last_synced_at": row["last_synced_at"],
            })
        return out

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #
    def sync_source(self, source_id: str) -> SyncReport:
        source = self.storage.get_source(source_id)
        if source is None:
            raise KeyError(f"unknown source {source_id}")
        options = json.loads(source["options_json"] or "{}")

        try:
            fetcher = self.fetchers.get(source["type"])
            fetched = fetcher.fetch(source["uri"], options)
        except Exception as e:
            # Deliberately broad: a connector failure — missing dependency, expired
            # credential, DNS error, malformed response — must degrade to reported
            # source health, never take down a sync or a run (§4.1.5).
            message = f"{type(e).__name__}: {e}" if not isinstance(e, FetchError) else str(e)
            self.storage.set_source_status(source_id, "error", message)
            return SyncReport(source_id, ok=False, error=message)

        report = SyncReport(source_id, ok=True)
        seen_docs: set[str] = set()
        errors: list[str] = []

        for doc in fetched:
            doc_id = _doc_id(source_id, doc.external_id)
            seen_docs.add(doc_id)
            content_hash = hashlib.sha256(doc.data).hexdigest()
            existing = self.storage.get_doc(doc_id)
            if existing and existing["content_hash"] == content_hash and not existing["tombstoned_at"]:
                report.docs_skipped += 1          # incremental sync: nothing changed
                continue
            try:
                segments = extract(doc.title, doc.data, doc.mime)
            except ExtractionError as e:
                errors.append(f"{doc.title}: {e}")
                continue

            self.storage.upsert_doc(doc_id, source_id, doc.external_id, doc.title,
                                    doc.mime, content_hash, len(doc.data))
            rows = self._chunk_and_embed(segments)
            self.storage.replace_chunks(doc_id, rows)
            report.docs_indexed += 1
            report.chunks += len(rows)

        # Documents that vanished from the source are tombstoned, not silently kept.
        for existing in self.storage.docs_for_source(source_id):
            if existing["id"] not in seen_docs:
                self.storage.tombstone_doc(existing["id"])
                report.docs_tombstoned += 1

        if errors and report.docs_indexed == 0 and report.docs_skipped == 0:
            report.ok = False
            report.error = "; ".join(errors)
            self.storage.set_source_status(source_id, "error", report.error)
        elif errors:
            report.error = "; ".join(errors)          # partial success, still surfaced
            self.storage.set_source_status(source_id, "ok", report.error)
        else:
            self.storage.set_source_status(source_id, "ok", "")
        return report

    def sync_all(self, agent: Optional[str] = None) -> list[SyncReport]:
        rows = self.storage.sources_for(agent) if agent else self.storage.list_sources()
        return [self.sync_source(r["id"]) for r in rows]

    def _chunk_and_embed(self, segments) -> list[tuple[str, str, int, str]]:
        texts: list[str] = []
        locators: list[str] = []
        for seg in segments:
            for piece in chunk_segment(seg.text):
                texts.append(piece)
                locators.append(seg.locator)
        if not texts:
            return []
        vectors = self.embedding.embed(texts)
        return [
            (text, loc, approx_tokens(text), json.dumps(vec))
            for text, loc, vec in zip(texts, locators, vectors)
        ]

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    def retrieve(self, agent: str, query: str, k: int = 5) -> list[Passage]:
        """Top-k cited passages visible to `agent` (own sources + team sources)."""
        query = (query or "").strip()
        if not query:
            return []
        rows = self.storage.chunks_for_agent(agent)
        if not rows:
            return []

        qvec = self.embedding.embed([query])[0]
        qterms = set(tokenize(query))
        team_sources = {
            r["id"] for r in self.storage.list_sources() if r["scope"] == "team"
        }

        scored: list[tuple[float, Passage]] = []
        for row in rows:
            try:
                vec = json.loads(row["embedding"]) if row["embedding"] else []
            except (TypeError, ValueError):
                vec = []
            score = W_VECTOR * cosine(qvec, vec) + W_KEYWORD * keyword_overlap(qterms, row["text"])
            if score <= 0:
                continue
            if row["source_id"] in team_sources:
                score *= TEAM_SCOPE_PENALTY
            scored.append((
                score,
                Passage(
                    source_id=row["source_id"],
                    doc=row["doc_title"] or "",
                    locator=row["locator"] or "",
                    text=row["text"],
                    score=round(score, 6),
                ),
            ))

        scored.sort(key=lambda x: x[0], reverse=True)
        # De-duplicate identical passages that appear in more than one source.
        out: list[Passage] = []
        seen: set[str] = set()
        for _, passage in scored:
            key = passage.text[:200]
            if key in seen:
                continue
            seen.add(key)
            out.append(passage)
            if len(out) >= k:
                break
        return out


def _doc_id(source_id: str, external_id: str) -> str:
    digest = hashlib.sha256(f"{source_id}\n{external_id}".encode()).hexdigest()[:16]
    return f"doc_{digest}"
