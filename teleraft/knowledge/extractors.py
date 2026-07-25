"""Format extractors (DESIGN.md §4.1.2).

Every extractor turns raw bytes into ``Segment(text, locator)`` pairs. The **locator**
is what makes a citation useful — `p.12`, `# Brand > ## Tone`, `row 4` — so a reviewer
can jump straight to the passage a claim came from.

Format rules that matter:
  .md   — split on heading boundaries; locator is the heading path
  .pdf  — one segment per page; locator is the page number (needs `pypdf`)
  .csv  — header-aware; **whole rows are never split** (a split row is a corrupted fact)
  .txt  — paragraph boundaries
  html  — boilerplate stripped, title kept
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass


class ExtractionError(Exception):
    """Raised when a document cannot be turned into text (reported as source health)."""


@dataclass
class Segment:
    text: str
    locator: str


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def extract_markdown(data: bytes) -> list[Segment]:
    lines = data.decode("utf-8", errors="replace").splitlines()
    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []          # (level, title)
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            path = " > ".join(f"{'#' * lvl} {title}" for lvl, title in stack) or "(preamble)"
            segments.append(Segment(text=body, locator=path))
        buf.clear()

    for line in lines:
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            # Keep the heading itself in the body so the passage reads standalone.
            buf.append(line.strip())
        else:
            buf.append(line)
    flush()
    return segments


# --------------------------------------------------------------------------- #
# Plain text
# --------------------------------------------------------------------------- #
def extract_text(data: bytes) -> list[Segment]:
    text = data.decode("utf-8", errors="replace")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [Segment(text=p, locator=f"¶{i + 1}") for i, p in enumerate(paras)]


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def extract_csv(data: bytes, max_rows: int = 5000) -> list[Segment]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as e:
        raise ExtractionError(f"malformed CSV: {e}") from e
    if not rows:
        return []

    header = [h.strip() for h in rows[0]]
    segments = [
        Segment(
            text="Columns: " + ", ".join(header),
            locator="schema",
        )
    ]
    for i, row in enumerate(rows[1:max_rows + 1], start=1):
        if not any(cell.strip() for cell in row):
            continue
        # Render a row as "col: value" pairs so a retrieved row is self-describing —
        # and keep it whole in a single segment.
        pairs = [
            f"{header[j] if j < len(header) else f'col{j+1}'}: {cell.strip()}"
            for j, cell in enumerate(row)
            if cell.strip()
        ]
        segments.append(Segment(text="; ".join(pairs), locator=f"row {i}"))
    if len(rows) - 1 > max_rows:
        segments.append(
            Segment(text=f"(truncated: {len(rows) - 1 - max_rows} further rows not indexed)",
                    locator="notice")
        )
    return segments


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def extract_pdf(data: bytes) -> list[Segment]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - optional dependency
        raise ExtractionError(
            "PDF support requires pypdf: pip install teleraft[knowledge]"
        ) from e
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise ExtractionError(f"unreadable PDF: {e}") from e
    if getattr(reader, "is_encrypted", False):
        raise ExtractionError("encrypted PDF: decrypt it before adding as a source")

    segments: list[Segment] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            segments.append(Segment(text=text, locator=f"p.{i}"))
    if not segments:
        # Scanned/image PDF: say so instead of indexing an empty document (§4.1.2).
        raise ExtractionError("no extractable text (scanned PDF?) — needs OCR, not supported in v1")
    return segments


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_SCRIPT_STYLE = re.compile(r"<(script|style|nav|footer|header)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def extract_html(data: bytes) -> list[Segment]:
    html = data.decode("utf-8", errors="replace")
    title_m = _TITLE.search(html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    body = _SCRIPT_STYLE.sub(" ", html)
    # Turn block boundaries into paragraph breaks before stripping tags.
    body = re.sub(r"</(p|div|section|article|li|h[1-6]|tr)>", "\n\n", body, flags=re.I)
    body = _TAG.sub(" ", body)
    body = _unescape(body)
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", body)]
    paras = [p for p in paras if len(p) > 40]        # drop nav crumbs and stray labels
    loc = title or "page"
    return [Segment(text=p, locator=f"{loc} ¶{i + 1}") for i, p in enumerate(paras)]


def _unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_BY_SUFFIX = {
    ".md": extract_markdown,
    ".markdown": extract_markdown,
    ".txt": extract_text,
    ".csv": extract_csv,
    ".pdf": extract_pdf,
    ".html": extract_html,
    ".htm": extract_html,
}

SUPPORTED_SUFFIXES = tuple(_BY_SUFFIX)


def extract(name: str, data: bytes, mime: str = "") -> list[Segment]:
    """Extract citable segments from a document, dispatching on suffix then MIME."""
    lowered = name.lower()
    for suffix, fn in _BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return fn(data)

    mime = (mime or "").lower()
    if "pdf" in mime:
        return extract_pdf(data)
    if "csv" in mime:
        return extract_csv(data)
    if "html" in mime:
        return extract_html(data)
    if "markdown" in mime:
        return extract_markdown(data)
    if mime.startswith("text/"):
        return extract_text(data)
    raise ExtractionError(
        f"unsupported document type for {name!r} (mime={mime!r}); "
        f"supported: {', '.join(SUPPORTED_SUFFIXES)}"
    )
