"""Google Books Metadata & Cover Artwork Fetcher.

Fetches book description and high-resolution cover artwork from Google Books API
without requiring an API key.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
import httpx

logger = logging.getLogger(__name__)


@dataclass
class FetchedMetadata:
    title: str
    author: str
    description: str = ""
    isbn: str = ""
    cover_image_bytes: bytes | None = None


class MetadataFetcher:
    """Fetch book metadata and cover images from Google Books API."""

    @staticmethod
    def fetch(title: str, author: str) -> FetchedMetadata:
        """Fetch metadata for a given book title and author.

        Args:
            title: Book title.
            author: Book author.

        Returns:
            FetchedMetadata object.
        """
        logger.info("Querying Google Books API for '%s' by '%s'", title, author)
        query = f"intitle:{title}"
        if author:
            query += f"+inauthor:{author}"

        encoded_query = urllib.parse.quote(query)
        url = f"https://www.googleapis.com/books/v1/volumes?q={encoded_query}"

        description = ""
        isbn = ""
        cover_bytes = None

        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

                items = data.get("items", [])
                if items:
                    info = items[0].get("volumeInfo", {})
                    description = info.get("description", "")

                    # Extract ISBN if present
                    identifiers = info.get("industryIdentifiers", [])
                    for ident in identifiers:
                        if ident.get("type") in ("ISBN_13", "ISBN_10"):
                            isbn = ident.get("identifier", "")
                            break

                    # Get cover image
                    image_links = info.get("imageLinks", {})
                    image_url = (
                        image_links.get("extraLarge")
                        or image_links.get("large")
                        or image_links.get("medium")
                        or image_links.get("thumbnail")
                        or image_links.get("smallThumbnail")
                    )

                    if image_url:
                        # Upgrade http to https and increase resolution param if possible
                        image_url = image_url.replace("http://", "https://")
                        image_url = image_url.replace("zoom=1", "zoom=3")
                        logger.info("Downloading cover image from %s", image_url)
                        img_resp = client.get(image_url)
                        if img_resp.status_code == 200:
                            cover_bytes = img_resp.content

        except Exception as e:
            logger.warning("Failed to fetch metadata from Google Books API: %s", e)

        return FetchedMetadata(
            title=title,
            author=author,
            description=description,
            isbn=isbn,
            cover_image_bytes=cover_bytes,
        )
