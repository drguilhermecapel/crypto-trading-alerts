from __future__ import annotations

import unittest
from io import BytesIO

from crypto_alerts.http import PublicSourceError, fetch_bytes, fetch_json


class FakeResponse:
    def __init__(self, payload: bytes, url: str = "https://example.com/feed") -> None:
        self.payload = BytesIO(payload)
        self.status = 200
        self.url = url

    def read(self, size: int = -1) -> bytes:
        return self.payload.read(size)

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class HttpTests(unittest.TestCase):
    def test_bounded_https_fetch_and_json(self) -> None:
        captured = {}

        def opener(outgoing, timeout):
            captured["scheme"] = outgoing.full_url.split(":", 1)[0]
            captured["timeout"] = timeout
            return FakeResponse(b'{"ok": true}')

        self.assertEqual(fetch_bytes("https://example.com/feed", urlopen=opener), b'{"ok": true}')
        self.assertEqual(fetch_json("https://example.com/feed", urlopen=opener), {"ok": True})
        self.assertEqual(captured, {"scheme": "https", "timeout": 12.0})

    def test_local_or_oversized_sources_fail_closed(self) -> None:
        with self.assertRaises(PublicSourceError):
            fetch_bytes("https://127.0.0.1/feed")
        with self.assertRaises(PublicSourceError):
            fetch_bytes(
                "https://example.com/feed",
                max_bytes=3,
                urlopen=lambda *args, **kwargs: FakeResponse(b"four"),
            )

    def test_redirect_to_non_https_is_rejected(self) -> None:
        with self.assertRaises(PublicSourceError):
            fetch_bytes(
                "https://example.com/feed",
                urlopen=lambda *args, **kwargs: FakeResponse(b"ok", "http://example.com/feed"),
            )


if __name__ == "__main__":
    unittest.main()
