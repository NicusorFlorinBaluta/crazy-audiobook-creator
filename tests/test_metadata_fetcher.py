from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

import httpx

from brain.extractor.metadata_fetcher import MetadataFetcher


def _png(width: int = 320, height: int = 480) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height)


class MetadataFetcherTests(unittest.TestCase):
    def test_configured_api_key_is_sent_without_affecting_the_query(self) -> None:
        observed_key = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal observed_key
            observed_key = request.url.params.get("key")
            return httpx.Response(200, json={"items": []})

        with patch.dict(
            "brain.extractor.metadata_fetcher.os.environ",
            {"GOOGLE_BOOKS_API_KEY": "test-secret-key"},
            clear=False,
        ):
            result = MetadataFetcher.fetch(
                "Expected",
                "Author",
                transport=httpx.MockTransport(handler),
            )

        self.assertEqual(result.status, "no_match")
        self.assertEqual(observed_key, "test-secret-key")

    def test_ranks_candidates_and_extracts_structured_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.googleapis.com":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "wrong",
                                "volumeInfo": {
                                    "title": "A Different Book",
                                    "authors": ["Someone Else"],
                                },
                            },
                            {
                                "id": "exact",
                                "volumeInfo": {
                                    "title": "The Sample Book",
                                    "authors": ["Ada Author"],
                                    "description": "A useful synopsis.",
                                    "industryIdentifiers": [
                                        {"type": "ISBN_13", "identifier": "9780000000001"}
                                    ],
                                    "categories": ["Science Fiction"],
                                    "publishedDate": "2025-04-03",
                                    "language": "en",
                                    "imageLinks": {
                                        "thumbnail": "https://books.google.com/cover?zoom=1"
                                    },
                                },
                            },
                        ]
                    },
                )
            return httpx.Response(
                200,
                content=_png(),
                headers={"content-type": "image/png"},
            )

        result = MetadataFetcher.fetch(
            "The Sample Book",
            "Ada Author",
            "en",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.provider_id, "exact")
        self.assertEqual(result.isbn, "9780000000001")
        self.assertEqual(result.genre, "Science Fiction")
        self.assertEqual(result.year, "2025")
        self.assertEqual((result.cover_width, result.cover_height), (320, 480))

    def test_no_close_match_is_explicit(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"items": [{"id": "x", "volumeInfo": {"title": "Unrelated"}}]},
            )
        )
        result = MetadataFetcher.fetch("Expected", "Author", transport=transport)
        self.assertEqual(result.status, "no_match")
        self.assertIn("No sufficiently close", result.error)

    def test_exact_title_with_contradictory_author_is_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"items": [{
                    "id": "wrong-author",
                    "volumeInfo": {"title": "Expected", "authors": ["Entirely Different"]},
                }]},
            )
        )
        result = MetadataFetcher.fetch("Expected", "Ada Author", transport=transport)
        self.assertEqual(result.status, "no_match")

    def test_provider_failure_retries_and_is_not_reported_as_success(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="temporarily unavailable")

        with patch("brain.extractor.metadata_fetcher.time.sleep"):
            result = MetadataFetcher.fetch(
                "Expected",
                "Author",
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(calls, 3)
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(
            result.error,
            "Google Books is temporarily unavailable. Try again later.",
        )
        self.assertNotIn("https://", result.error)

    def test_rate_limit_is_not_retried_and_honors_retry_after(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"retry-after": "120"})

        result = MetadataFetcher.fetch(
            "Expected",
            "Author",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(
            result.error,
            "Google Books rate limit reached. Try again after 120 seconds.",
        )

    def test_unapproved_cover_host_is_rejected_without_losing_match(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "exact",
                        "volumeInfo": {
                            "title": "Expected",
                            "authors": ["Author"],
                            "imageLinks": {"thumbnail": "https://example.com/cover.jpg"},
                        },
                    }]
                },
            )
        )
        result = MetadataFetcher.fetch("Expected", "Author", transport=transport)
        self.assertEqual(result.status, "matched")
        self.assertIsNone(result.cover_image_bytes)
        self.assertTrue(any("approved Google" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
