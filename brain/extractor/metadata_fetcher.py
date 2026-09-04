"""Google Books metadata lookup with deterministic matching and cover checks."""

from __future__ import annotations

import logging
import os
import re
import struct
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
_MAX_RESULTS = 10
_MATCH_THRESHOLD = 0.72
_MAX_COVER_BYTES = 8 * 1024 * 1024
_MAX_COVER_PIXELS = 40_000_000
_ALLOWED_COVER_HOSTS = {"books.google.com"}
_ALLOWED_COVER_SUFFIXES = (".googleusercontent.com",)


@dataclass
class FetchedMetadata:
    status: Literal["matched", "no_match", "provider_error"]
    query_title: str
    query_author: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    description: str = ""
    isbn: str = ""
    genre: str = ""
    year: str = ""
    language: str = ""
    provider: str = "google_books"
    provider_id: str = ""
    confidence: float = 0.0
    cover_image_bytes: bytes | None = None
    cover_mime_type: str = ""
    cover_width: int | None = None
    cover_height: int | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def author(self) -> str:
        return ", ".join(self.authors)

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("cover_image_bytes", None)
        payload["author"] = self.author
        payload["has_cover"] = self.cover_image_bytes is not None
        return payload


@dataclass
class MetadataSearchResults:
    status: Literal["matched", "no_match", "provider_error"]
    query_title: str
    query_author: str
    results: list[FetchedMetadata] = field(default_factory=list)
    error: str = ""

    def serializable(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query_title": self.query_title,
            "query_author": self.query_author,
            "results": [result.serializable() for result in self.results],
            "error": self.error,
        }


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _similarity(expected: str, actual: str, *, title: bool = False) -> float:
    left = _normalize(expected)
    right = _normalize(actual)
    if not left or not right:
        return 0.0
    variants_left = {left}
    variants_right = {right}
    if title:
        variants_left.add(_normalize(str(expected).split(":", 1)[0]))
        variants_right.add(_normalize(str(actual).split(":", 1)[0]))
    best = 0.0
    for left_variant in variants_left:
        for right_variant in variants_right:
            if not left_variant or not right_variant:
                continue
            if left_variant == right_variant:
                return 1.0
            ratio = SequenceMatcher(None, left_variant, right_variant).ratio()
            left_tokens = set(left_variant.split())
            right_tokens = set(right_variant.split())
            union = left_tokens | right_tokens
            token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
            best = max(best, ratio, token_score)
    return best


def _candidate_score(
    query_title: str,
    query_author: str,
    info: dict[str, Any],
) -> float:
    title_score = _similarity(query_title, str(info.get("title", "")), title=True)
    if not query_author:
        return title_score
    authors = info.get("authors") or []
    if not isinstance(authors, list):
        authors = []
    author_score = max(
        (_similarity(query_author, str(author)) for author in authors),
        default=0.0,
    )
    # An exact title alone is not sufficient when the provider explicitly
    # contradicts the requested author.
    if author_score < 0.45:
        return title_score * 0.5
    return (title_score * 0.72) + (author_score * 0.28)


def _cover_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("cover payload is not a valid PNG")
        width, height = struct.unpack(">II", content[16:24])
        return width, height

    if mime_type in {"image/jpeg", "image/jpg"}:
        if len(content) < 4 or content[:2] != b"\xff\xd8":
            raise ValueError("cover payload is not a valid JPEG")
        offset = 2
        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while offset + 8 < len(content):
            if content[offset] != 0xFF:
                offset += 1
                continue
            marker = content[offset + 1]
            if marker in {0xD8, 0xD9}:
                offset += 2
                continue
            if offset + 4 > len(content):
                break
            segment_length = int.from_bytes(content[offset + 2 : offset + 4], "big")
            if segment_length < 2 or offset + 2 + segment_length > len(content):
                break
            if marker in sof_markers and segment_length >= 7:
                height = int.from_bytes(content[offset + 5 : offset + 7], "big")
                width = int.from_bytes(content[offset + 7 : offset + 9], "big")
                return width, height
            offset += 2 + segment_length
        raise ValueError("JPEG cover has no readable dimensions")

    raise ValueError(f"unsupported cover content type: {mime_type or 'missing'}")


def _validate_cover_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("cover URL must use HTTPS")
    if host not in _ALLOWED_COVER_HOSTS and not host.endswith(_ALLOWED_COVER_SUFFIXES):
        raise ValueError("cover URL host is not an approved Google image host")
    return parsed.geturl()


def _download_cover(
    client: httpx.Client,
    url: str,
) -> tuple[bytes, str, int, int]:
    safe_url = _validate_cover_url(url)
    with client.stream("GET", safe_url) as response:
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        declared_length = int(response.headers.get("content-length") or 0)
        if declared_length > _MAX_COVER_BYTES:
            raise ValueError("cover exceeds the 8 MB limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_COVER_BYTES:
                raise ValueError("cover exceeds the 8 MB limit")
            chunks.append(chunk)
    content = b"".join(chunks)
    width, height = _cover_dimensions(content, mime_type)
    if width < 80 or height < 80:
        raise ValueError("cover dimensions are too small")
    if width > 10_000 or height > 10_000 or width * height > _MAX_COVER_PIXELS:
        raise ValueError("cover dimensions exceed safety limits")
    return content, mime_type, width, height


def _request_search(client: httpx.Client, params: dict[str, str | int]) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(_GOOGLE_BOOKS_URL, params=params)
            # Quota exhaustion normally lasts longer than an interactive
            # retry loop. Return it immediately instead of multiplying the
            # rejected traffic; callers surface an actionable message.
            if response.status_code == 429:
                response.raise_for_status()
            if response.status_code >= 500:
                response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                break
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _search_params(
    title: str,
    author: str,
    language: str,
) -> dict[str, str | int]:
    query = f'intitle:"{title}"'
    if author:
        query += f' inauthor:"{author}"'
    params: dict[str, str | int] = {
        "q": query,
        "maxResults": _MAX_RESULTS,
        "printType": "books",
        "orderBy": "relevance",
        "fields": (
            "items(id,volumeInfo(title,subtitle,authors,description,"
            "industryIdentifiers,categories,publishedDate,language,imageLinks))"
        ),
    }
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key
    normalized_language = str(language or "").strip().lower()
    if re.fullmatch(r"[a-z]{2}", normalized_language):
        params["langRestrict"] = normalized_language
    return params


def _metadata_from_item(
    item: dict[str, Any],
    query_title: str,
    query_author: str,
    *,
    confidence: float,
) -> FetchedMetadata:
    info = item.get("volumeInfo") or {}
    if not isinstance(info, dict):
        info = {}
    raw_identifiers = info.get("industryIdentifiers") or []
    if not isinstance(raw_identifiers, list):
        raw_identifiers = []
    identifiers = {
        str(identifier.get("type", "")): str(identifier.get("identifier", ""))
        for identifier in raw_identifiers
        if isinstance(identifier, dict)
    }
    categories = info.get("categories") or []
    if not isinstance(categories, list):
        categories = []
    authors = info.get("authors") or []
    if not isinstance(authors, list):
        authors = []
    published_date = str(info.get("publishedDate") or "")
    year_match = re.match(r"\d{4}", published_date)
    return FetchedMetadata(
        status="matched",
        query_title=query_title,
        query_author=query_author,
        title=str(info.get("title") or query_title),
        authors=[str(value) for value in authors],
        description=str(info.get("description") or ""),
        isbn=identifiers.get("ISBN_13") or identifiers.get("ISBN_10") or "",
        genre=str(categories[0]) if categories else "",
        year=year_match.group(0) if year_match else "",
        language=str(info.get("language") or ""),
        provider_id=str(item.get("id") or ""),
        confidence=round(confidence, 4),
    )


def _attach_cover(
    result: FetchedMetadata,
    item: dict[str, Any],
    client: httpx.Client,
) -> None:
    info = item.get("volumeInfo") or {}
    image_links = (info.get("imageLinks") or {}) if isinstance(info, dict) else {}
    if not isinstance(image_links, dict):
        image_links = {}
    image_url = next(
        (
            image_links.get(key)
            for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail")
            if image_links.get(key)
        ),
        None,
    )
    if not image_url:
        return
    try:
        image_url = str(image_url).replace("http://", "https://", 1)
        image_url = image_url.replace("zoom=1", "zoom=3")
        (
            result.cover_image_bytes,
            result.cover_mime_type,
            result.cover_width,
            result.cover_height,
        ) = _download_cover(client, image_url)
    except Exception as exc:
        result.warnings.append(f"Cover was rejected: {exc}")
        logger.warning("Rejected Google Books cover: %s", exc)


def _provider_error_message(exc: Exception) -> str:
    """Return an actionable message without leaking request internals."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            retry_after = str(exc.response.headers.get("retry-after") or "").strip()
            suffix = f" Try again after {retry_after} seconds." if retry_after.isdigit() else " Try again later."
            return "Google Books rate limit reached." + suffix
        if status_code >= 500:
            return "Google Books is temporarily unavailable. Try again later."
        return f"Google Books rejected the request (HTTP {status_code})."
    if isinstance(exc, httpx.TimeoutException):
        return "Google Books did not respond in time. Try again later."
    if isinstance(exc, httpx.TransportError):
        return "Could not connect to Google Books. Check the network and try again."
    return "Google Books returned an invalid response. Try again later."


class MetadataFetcher:
    """Fetch and validate a likely Google Books volume match."""

    @staticmethod
    def fetch(
        title: str,
        author: str,
        language: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> FetchedMetadata:
        query_title = str(title or "").strip()
        query_author = str(author or "").strip()
        if not query_title:
            return FetchedMetadata(
                status="no_match",
                query_title=query_title,
                query_author=query_author,
                error="A title is required for metadata lookup.",
            )

        params = _search_params(query_title, query_author, language)
        if "key" not in params and transport is None:
            logger.warning("GOOGLE_BOOKS_API_KEY is not configured; lookup may use a shared anonymous quota")
        normalized_language = str(language or "").strip().lower()
        if re.fullmatch(r"[a-z]{2}", normalized_language):
            params["langRestrict"] = normalized_language

        logger.info(
            "Querying Google Books API for %r by %r",
            query_title,
            query_author,
        )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "CrazyAudiobookCreator/1.0"},
            ) as client:
                response = _request_search(client, params)
                response.raise_for_status()
                data = response.json()
                ranked: list[tuple[float, dict[str, Any]]] = []
                for item in data.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    info = item.get("volumeInfo") or {}
                    if not isinstance(info, dict):
                        continue
                    ranked.append((_candidate_score(query_title, query_author, info), item))
                ranked.sort(key=lambda pair: pair[0], reverse=True)
                if not ranked or ranked[0][0] < _MATCH_THRESHOLD:
                    confidence = ranked[0][0] if ranked else 0.0
                    return FetchedMetadata(
                        status="no_match",
                        query_title=query_title,
                        query_author=query_author,
                        confidence=round(confidence, 4),
                        error="No sufficiently close Google Books match was found.",
                    )

                confidence, item = ranked[0]
                result = _metadata_from_item(
                    item,
                    query_title,
                    query_author,
                    confidence=confidence,
                )
                _attach_cover(result, item, client)
                return result
        except Exception as exc:
            safe_error = _provider_error_message(exc)
            # Never log the raw HTTP exception: its URL may contain the API key.
            logger.warning("Google Books metadata lookup failed: %s", safe_error)
            return FetchedMetadata(
                status="provider_error",
                query_title=query_title,
                query_author=query_author,
                error=safe_error,
            )

    @staticmethod
    def search(
        title: str,
        author: str = "",
        language: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> MetadataSearchResults:
        """Return ranked candidates for explicit human selection."""
        query_title = str(title or "").strip()
        query_author = str(author or "").strip()
        if not query_title:
            return MetadataSearchResults(
                status="no_match",
                query_title=query_title,
                query_author=query_author,
                error="A title is required for metadata lookup.",
            )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "CrazyAudiobookCreator/1.0"},
            ) as client:
                response = _request_search(
                    client,
                    _search_params(query_title, query_author, language),
                )
                response.raise_for_status()
                data = response.json()
                ranked: list[tuple[float, dict[str, Any]]] = []
                for item in data.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    info = item.get("volumeInfo") or {}
                    if not isinstance(info, dict) or not item.get("id"):
                        continue
                    ranked.append((_candidate_score(query_title, query_author, info), item))
                ranked.sort(key=lambda pair: pair[0], reverse=True)
                results = [
                    _metadata_from_item(
                        item,
                        query_title,
                        query_author,
                        confidence=confidence,
                    )
                    for confidence, item in ranked
                ]
                if not results:
                    return MetadataSearchResults(
                        status="no_match",
                        query_title=query_title,
                        query_author=query_author,
                        error="Google Books returned no results for this search.",
                    )
                return MetadataSearchResults(
                    status="matched",
                    query_title=query_title,
                    query_author=query_author,
                    results=results,
                )
        except Exception as exc:
            safe_error = _provider_error_message(exc)
            logger.warning("Google Books manual search failed: %s", safe_error)
            return MetadataSearchResults(
                status="provider_error",
                query_title=query_title,
                query_author=query_author,
                error=safe_error,
            )

    @staticmethod
    def fetch_volume(
        provider_id: str,
        query_title: str = "",
        query_author: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> FetchedMetadata:
        """Fetch the exact Google Books volume selected by the user."""
        volume_id = str(provider_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", volume_id):
            return FetchedMetadata(
                status="no_match",
                query_title=query_title,
                query_author=query_author,
                error="The selected Google Books volume ID is invalid.",
            )
        params: dict[str, str] = {}
        api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
        if api_key:
            params["key"] = api_key
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "CrazyAudiobookCreator/1.0"},
            ) as client:
                response = client.get(f"{_GOOGLE_BOOKS_URL}/{volume_id}", params=params)
                response.raise_for_status()
                item = response.json()
                if not isinstance(item, dict) or str(item.get("id") or "") != volume_id:
                    raise ValueError("Google Books returned an invalid volume")
                info = item.get("volumeInfo") or {}
                confidence = _candidate_score(query_title, query_author, info)
                result = _metadata_from_item(
                    item,
                    query_title,
                    query_author,
                    confidence=confidence,
                )
                _attach_cover(result, item, client)
                return result
        except Exception as exc:
            safe_error = _provider_error_message(exc)
            logger.warning("Google Books selected-volume fetch failed: %s", safe_error)
            return FetchedMetadata(
                status="provider_error",
                query_title=query_title,
                query_author=query_author,
                error=safe_error,
            )
