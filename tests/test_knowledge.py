"""Knowledge base tests: extractors, chunking, sync semantics, retrieval, scoping."""

import pytest

from teleraft.knowledge.embeddings import CHARS_PER_TOKEN, chunk_segment
from teleraft.knowledge.extractors import (
    ExtractionError,
    extract,
    extract_csv,
    extract_html,
    extract_markdown,
    extract_pdf,
    extract_text,
)
from teleraft.knowledge.fetchers import FetchError, LocalFileFetcher
from teleraft.knowledge.service import KnowledgeService
from teleraft.storage import Storage

from .conftest import make_pdf


# --------------------------------------------------------------------------- #
# Extractors — locators are what make citations useful
# --------------------------------------------------------------------------- #
def test_markdown_locator_is_the_heading_path():
    segs = extract_markdown(
        b"# Brand\nlead\n\n## Tone\nclear and concrete\n\n### Voice\nno hype\n"
    )
    locs = [s.locator for s in segs]
    assert "# Brand" in locs
    assert "# Brand > ## Tone" in locs
    assert "# Brand > ## Tone > ### Voice" in locs
    tone = next(s for s in segs if s.locator == "# Brand > ## Tone")
    assert "clear and concrete" in tone.text


def test_pdf_locator_is_the_page_number():
    data = make_pdf(["Refund policy is 30 days.", "Escalate wire transfers."])
    segs = extract_pdf(data)
    assert [s.locator for s in segs] == ["p.1", "p.2"]
    assert "Refund policy" in segs[0].text


def test_scanned_pdf_is_reported_not_silently_empty():
    # A structurally valid PDF whose pages carry no extractable text (i.e. a scan)
    # must raise, not index an empty document.
    with pytest.raises(ExtractionError, match="OCR"):
        extract_pdf(make_pdf([""]))


def test_malformed_pdf_is_reported_as_unreadable():
    with pytest.raises(ExtractionError, match="unreadable PDF"):
        extract_pdf(b"%PDF-1.4\nnot really a pdf\n")


def test_csv_keeps_rows_whole_and_describes_columns():
    segs = extract_csv(b"tier,price\nStarter,49\nGrowth,149\n")
    assert segs[0].locator == "schema" and "tier" in segs[0].text
    rows = [s for s in segs if s.locator.startswith("row")]
    assert [s.locator for s in rows] == ["row 1", "row 2"]
    # A row is self-describing and never split across chunks.
    assert "tier: Starter" in rows[0].text and "price: 49" in rows[0].text
    assert chunk_segment(rows[0].text) == [rows[0].text]


def test_text_and_html_extraction():
    segs = extract_text(b"para one\n\npara two\n")
    assert [s.locator for s in segs] == ["¶1", "¶2"]

    html = (b"<html><head><title>Docs</title></head><body><nav>skip me</nav>"
            b"<p>" + b"A meaningful paragraph about launch posts and registration links." + b"</p>"
            b"</body></html>")
    hsegs = extract_html(html)
    assert hsegs and hsegs[0].locator.startswith("Docs")
    assert "meaningful paragraph" in hsegs[0].text
    assert all("skip me" not in s.text for s in hsegs)


def test_unsupported_type_is_an_explicit_error():
    with pytest.raises(ExtractionError, match="unsupported"):
        extract("photo.png", b"\x89PNG", "image/png")


def test_chunking_respects_budget_with_overlap():
    long_text = "\n\n".join(f"Paragraph number {i} with some filler words." for i in range(300))
    chunks = chunk_segment(long_text, max_tokens=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 * CHARS_PER_TOKEN + 50 for c in chunks)


# --------------------------------------------------------------------------- #
# Fetcher confinement (§11 least privilege)
# --------------------------------------------------------------------------- #
def test_local_fetcher_refuses_to_escape_its_root(kb_dir):
    fetcher = LocalFileFetcher(str(kb_dir))
    with pytest.raises(FetchError, match="escapes"):
        fetcher.fetch("../../etc/passwd", {})


def test_local_fetcher_reads_a_directory_of_supported_files(kb_dir):
    got = LocalFileFetcher(str(kb_dir)).fetch("cole", {})
    names = sorted(f.title for f in got)
    assert names == ["brand.md", "notes.txt", "policy.pdf", "tiers.csv"]


# --------------------------------------------------------------------------- #
# Service: sync semantics and retrieval
# --------------------------------------------------------------------------- #
def _service(kb_dir):
    st = Storage(":memory:")
    return st, KnowledgeService(st, knowledge_root=str(kb_dir))


def test_ingest_then_incremental_sync_skips_unchanged(kb_dir):
    st, kb = _service(kb_dir)
    sid = kb.add_source("Cole", "file", "cole")
    first = kb.sync_source(sid)
    assert first.ok and first.docs_indexed == 4 and first.chunks > 0

    second = kb.sync_source(sid)
    assert second.docs_skipped == 4 and second.docs_indexed == 0   # content hash unchanged


def test_changed_document_is_reindexed_and_removed_one_is_tombstoned(kb_dir):
    st, kb = _service(kb_dir)
    sid = kb.add_source("Cole", "file", "cole")
    kb.sync_source(sid)

    (kb_dir / "cole" / "brand.md").write_text("# Brand\n\n## Tone\nPlayful and loud now.\n")
    report = kb.sync_source(sid)
    assert report.docs_indexed == 1 and report.docs_skipped == 3
    hits = kb.retrieve("Cole", "playful loud tone", k=1)
    assert hits and "Playful" in hits[0].text

    (kb_dir / "cole" / "notes.txt").unlink()
    report = kb.sync_source(sid)
    assert report.docs_tombstoned == 1
    assert all("newsletters" not in p.text for p in kb.retrieve("Cole", "newsletters", k=5))


def test_unreachable_source_is_marked_error_not_silently_empty(kb_dir):
    st, kb = _service(kb_dir)
    sid = kb.add_source("Cole", "file", "does-not-exist")
    report = kb.sync_source(sid)
    assert not report.ok and "no such file" in report.error
    health = {h["id"]: h for h in kb.health()}
    assert health[sid]["status"] == "error"


def test_retrieval_ranks_the_relevant_passage_first(kb_dir):
    st, kb = _service(kb_dir)
    kb.sync_source(kb.add_source("Cole", "file", "cole"))
    top = kb.retrieve("Cole", "what must a launch post include", k=3)
    assert top
    assert "registration link" in top[0].text
    assert top[0].cite().startswith("brand.md")


def test_retrieval_finds_pdf_and_csv_content_with_citable_locators(kb_dir):
    st, kb = _service(kb_dir)
    kb.sync_source(kb.add_source("Cole", "file", "cole"))

    pdf_hit = next(p for p in kb.retrieve("Cole", "refund policy days", k=5)
                   if p.doc == "policy.pdf")
    assert pdf_hit.locator == "p.1"

    csv_hit = next(p for p in kb.retrieve("Cole", "Growth tier price seats", k=5)
                   if p.doc == "tiers.csv")
    assert csv_hit.locator.startswith("row")
    assert "Growth" in csv_hit.text


def test_scoping_keeps_another_agents_sources_private(kb_dir):
    st, kb = _service(kb_dir)
    kb.sync_source(kb.add_source("Cole", "file", "cole/brand.md"))
    kb.sync_source(kb.add_source("Penn", "file", "cole/tiers.csv"))     # Penn-only
    kb.sync_source(kb.add_source("Cole", "file", "cole/notes.txt", scope="team"))

    penn_docs = {p.doc for p in kb.retrieve("Penn", "tier price Growth seats", k=5)}
    assert "tiers.csv" in penn_docs

    cole_docs = {p.doc for p in kb.retrieve("Cole", "tier price Growth seats", k=5)}
    assert "tiers.csv" not in cole_docs          # Penn's private source is invisible

    # …but the team-scoped source is visible to everyone.
    assert "notes.txt" in {p.doc for p in kb.retrieve("Penn", "webinars newsletters", k=5)}


def test_add_source_is_idempotent(kb_dir):
    st, kb = _service(kb_dir)
    a = kb.add_source("Cole", "file", "cole")
    b = kb.add_source("Cole", "file", "cole")
    assert a == b and len(kb.storage.list_sources()) == 1
