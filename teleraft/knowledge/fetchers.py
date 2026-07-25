"""Source fetchers: local files, web, Google Drive (DESIGN.md §4.1.1, §11).

Every fetcher returns ``Fetched`` documents (bytes + metadata); extraction, chunking,
and embedding are shared downstream. All connectors are **read-only** — TeleRaft never
writes to, renames, or deletes a source document.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urljoin, urlparse

from .extractors import SUPPORTED_SUFFIXES

MAX_DOC_BYTES = 10 * 1024 * 1024        # per-document size cap (§4.1.5)
MAX_DOCS_PER_SOURCE = 500


class FetchError(Exception):
    """Surfaces as source health, never as a silent gap (§4.1.5)."""


@dataclass
class Fetched:
    external_id: str          # url, drive fileId, or path
    title: str
    mime: str
    data: bytes


class SourceFetcher(Protocol):
    def fetch(self, uri: str, options: dict) -> list[Fetched]:
        ...


# --------------------------------------------------------------------------- #
# Local files
# --------------------------------------------------------------------------- #
class LocalFileFetcher:
    """Reads `.md` / `.pdf` / `.txt` / `.csv` from an allow-listed root directory.

    The root confinement is the §11 control: an agent's file sources cannot escape into
    arbitrary filesystem reads, even if a URI is crafted with `..`.
    """

    def __init__(self, root: str = "."):
        self.root = Path(root).resolve()

    def _resolve(self, uri: str) -> Path:
        path = Path(uri)
        candidate = (path if path.is_absolute() else self.root / path).resolve()
        if not str(candidate).startswith(str(self.root)):
            raise FetchError(f"path {uri!r} escapes the allowed knowledge root {self.root}")
        return candidate

    def fetch(self, uri: str, options: dict) -> list[Fetched]:
        target = self._resolve(uri)
        if not target.exists():
            raise FetchError(f"no such file or directory: {target}")

        paths: list[Path]
        if target.is_dir():
            pattern = "**/*" if options.get("recursive", True) else "*"
            paths = sorted(
                p for p in target.glob(pattern)
                if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
            )
        else:
            paths = [target]

        out: list[Fetched] = []
        for p in paths[:MAX_DOCS_PER_SOURCE]:
            size = p.stat().st_size
            if size > MAX_DOC_BYTES:
                raise FetchError(f"{p.name} is {size} bytes, over the {MAX_DOC_BYTES} cap")
            out.append(
                Fetched(
                    external_id=str(p),
                    title=p.name,
                    mime=_mime_for(p.name),
                    data=p.read_bytes(),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# Web
# --------------------------------------------------------------------------- #
class WebFetcher:
    """Fetches a URL, optionally following its sitemap.

    `options`: {"crawl": "none" | "sitemap", "max_pages": int, "respect_robots": bool}
    """

    def __init__(self, http=None, user_agent: str = "TeleRaft/0.2 (+knowledge-sync)"):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("WebFetcher needs httpx: pip install teleraft[telegram]") from e
        self._httpx = httpx
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)
        self.user_agent = user_agent

    def _get(self, url: str) -> tuple[bytes, str]:
        try:
            resp = self._http.get(url, headers={"User-Agent": self.user_agent})
        except self._httpx.HTTPError as e:
            # DNS failure, timeout, refused connection — source health, never a crash.
            raise FetchError(f"{url} unreachable: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {resp.status_code}")
        data = resp.content
        if len(data) > MAX_DOC_BYTES:
            raise FetchError(f"{url} is {len(data)} bytes, over the {MAX_DOC_BYTES} cap")
        return data, resp.headers.get("content-type", "").split(";")[0].strip()

    def _disallowed(self, url: str) -> set[str]:
        """Minimal robots.txt honouring: collect Disallow paths for '*' (§11)."""
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = self._http.get(robots_url, headers={"User-Agent": self.user_agent})
            if resp.status_code >= 400:
                return set()
            body = resp.text
        except Exception:
            return set()
        disallowed, applies = set(), False
        for line in body.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                applies = value == "*"
            elif key == "disallow" and applies and value:
                disallowed.add(value)
        return disallowed

    def fetch(self, uri: str, options: dict) -> list[Fetched]:
        respect_robots = options.get("respect_robots", True)
        blocked = self._disallowed(uri) if respect_robots else set()

        def allowed(url: str) -> bool:
            path = urlparse(url).path or "/"
            return not any(path.startswith(rule) for rule in blocked)

        if not allowed(uri):
            raise FetchError(f"{uri} is disallowed by robots.txt")

        urls = [uri]
        if options.get("crawl") == "sitemap":
            urls = self._sitemap_urls(uri, int(options.get("max_pages", 50))) or [uri]
            urls = [u for u in urls if allowed(u)]

        out: list[Fetched] = []
        for url in urls[:MAX_DOCS_PER_SOURCE]:
            data, mime = self._get(url)
            out.append(Fetched(external_id=url, title=_title_for(url), mime=mime or "text/html",
                               data=data))
        return out

    def _sitemap_urls(self, base: str, limit: int) -> list[str]:
        import re

        sitemap = urljoin(base, "/sitemap.xml")
        try:
            data, _ = self._get(sitemap)
        except FetchError:
            return []
        found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", data.decode("utf-8", "replace"))
        return found[:limit]


# --------------------------------------------------------------------------- #
# Google Drive (read-only)
# --------------------------------------------------------------------------- #
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}


class GoogleDriveFetcher:
    """Google Drive v3, **read-only** (`drive.readonly` scope or a shared service account).

    URIs: ``drive://folders/<id>`` or ``drive://files/<id>``. Google-native Docs/Sheets/
    Slides are exported to text/CSV; everything else is downloaded as-is and handled by
    the normal extractors.

    The access token is supplied by a callable so credential refresh stays outside this
    class (and out of the server's database — §11).
    """

    API = "https://www.googleapis.com/drive/v3"

    def __init__(self, access_token: Optional[str] = None, token_provider=None, http=None):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("GoogleDriveFetcher needs httpx: pip install teleraft[telegram]") from e
        if not access_token and token_provider is None:
            access_token = os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN")
        if not access_token and token_provider is None:
            raise FetchError(
                "Google Drive source needs an access token: set GOOGLE_DRIVE_ACCESS_TOKEN "
                "or pass token_provider (read-only scope)"
            )
        self._token_provider = token_provider or (lambda: access_token)
        self._http = http or httpx.Client(timeout=30)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def _json(self, url: str, params: dict) -> dict:
        try:
            resp = self._http.get(url, params=params, headers=self._headers())
        except Exception as e:
            raise FetchError(f"Google Drive unreachable: {type(e).__name__}: {e}") from e
        if resp.status_code == 401:
            raise FetchError("Google Drive token rejected (expired or revoked)")
        if resp.status_code >= 400:
            raise FetchError(f"Drive API {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def _parse(uri: str) -> tuple[str, str]:
        rest = uri.removeprefix("drive://")
        kind, _, ident = rest.partition("/")
        if kind not in ("folders", "files") or not ident:
            raise FetchError(f"bad Drive URI {uri!r}; expected drive://folders/<id> or drive://files/<id>")
        return kind, ident

    def _list_folder(self, folder_id: str, recursive: bool) -> list[dict]:
        files, stack, seen = [], [folder_id], set()
        while stack and len(files) < MAX_DOCS_PER_SOURCE:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            page_token = None
            while True:
                params = {
                    "q": f"'{current}' in parents and trashed = false",
                    "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                    "pageSize": 100,
                }
                if page_token:
                    params["pageToken"] = page_token
                payload = self._json(f"{self.API}/files", params)
                for f in payload.get("files", []):
                    if f["mimeType"] == "application/vnd.google-apps.folder":
                        if recursive:
                            stack.append(f["id"])
                    else:
                        files.append(f)
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
        return files

    def _download(self, meta: dict) -> Optional[Fetched]:
        mime = meta["mimeType"]
        name = meta.get("name", meta["id"])
        if mime in GOOGLE_EXPORTS:
            export_mime, suffix = GOOGLE_EXPORTS[mime]
            resp = self._http.get(
                f"{self.API}/files/{meta['id']}/export",
                params={"mimeType": export_mime},
                headers=self._headers(),
            )
            out_mime, out_name = export_mime, (name if name.endswith(suffix) else name + suffix)
        else:
            if not any(name.lower().endswith(s) for s in SUPPORTED_SUFFIXES):
                return None      # skip images, video, binaries — not a failure
            resp = self._http.get(
                f"{self.API}/files/{meta['id']}",
                params={"alt": "media"},
                headers=self._headers(),
            )
            out_mime, out_name = mime, name
        if resp.status_code >= 400:
            raise FetchError(f"Drive download of {name} failed: HTTP {resp.status_code}")
        data = resp.content
        if len(data) > MAX_DOC_BYTES:
            raise FetchError(f"{name} is {len(data)} bytes, over the {MAX_DOC_BYTES} cap")
        return Fetched(external_id=meta["id"], title=out_name, mime=out_mime, data=data)

    def fetch(self, uri: str, options: dict) -> list[Fetched]:
        kind, ident = self._parse(uri)
        if kind == "files":
            meta = self._json(f"{self.API}/files/{ident}", {"fields": "id, name, mimeType, size"})
            got = self._download(meta)
            return [got] if got else []
        metas = self._list_folder(ident, bool(options.get("recursive", True)))
        out: list[Fetched] = []
        for meta in metas:
            got = self._download(meta)
            if got:
                out.append(got)
        return out


# --------------------------------------------------------------------------- #
# Helpers / registry
# --------------------------------------------------------------------------- #
def _mime_for(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    if lowered.endswith((".html", ".htm")):
        return "text/html"
    return "text/plain"


def _title_for(url: str) -> str:
    parts = urlparse(url)
    return (parts.path.rsplit("/", 1)[-1] or parts.netloc) or url


@dataclass
class FetcherRegistry:
    """Maps a source `type` to its fetcher; built lazily so optional deps stay optional."""

    knowledge_root: str = "."
    _cache: dict = field(default_factory=dict)
    web_factory: Optional[object] = None
    drive_factory: Optional[object] = None

    def get(self, type_: str) -> SourceFetcher:
        if type_ in self._cache:
            return self._cache[type_]
        if type_ in ("file", "upload"):
            fetcher: SourceFetcher = LocalFileFetcher(self.knowledge_root)
        elif type_ == "web":
            fetcher = self.web_factory() if self.web_factory else WebFetcher()
        elif type_ == "gdrive":
            fetcher = self.drive_factory() if self.drive_factory else GoogleDriveFetcher()
        else:
            raise FetchError(f"unknown source type {type_!r}")
        self._cache[type_] = fetcher
        return fetcher
