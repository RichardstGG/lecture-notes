"""Tests for streamed browser audio staging."""
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.backend.upload_store import AudioUploadStore, UploadStoreError


async def chunks(*values):
    for value in values:
        yield value


class AudioUploadStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "uploads"
        self.store = AudioUploadStore(self.root, max_bytes=8)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_streams_supported_media_to_unique_local_path(self):
        result = await self.store.save(
            "課堂錄音.OGG", chunks(b"abc", b"def"), content_length="6",
        )

        target = Path(result["path"])
        self.assertEqual(result["api_version"], 1)
        self.assertEqual(result["name"], "課堂錄音.OGG")
        self.assertEqual(result["size_bytes"], 6)
        self.assertEqual(target.parent, self.root.resolve())
        self.assertEqual(target.suffix, ".ogg")
        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertFalse(list(self.root.glob("*.part")))

    async def test_rejects_unsafe_or_unsupported_names(self):
        for name, code in (
            ("../lecture.ogg", "invalid_upload_name"),
            ("lecture.txt", "unsupported_media_type"),
        ):
            with self.subTest(name=name), self.assertRaises(UploadStoreError) as ctx:
                await self.store.save(name, chunks(b"abc"))
            self.assertEqual(ctx.exception.code, code)
        self.assertFalse(self.root.exists())

    async def test_rejects_invalid_length_oversize_and_empty_uploads(self):
        cases = (
            ("invalid.wav", chunks(b"x"), "nope", "invalid_content_length"),
            ("too-big.wav", chunks(b"x"), 9, "upload_too_large"),
            ("streamed.wav", chunks(b"12345", b"6789"), None, "upload_too_large"),
            ("empty.wav", chunks(b""), 0, "empty_upload"),
        )
        for name, body, length, code in cases:
            with self.subTest(name=name), self.assertRaises(UploadStoreError) as ctx:
                await self.store.save(name, body, content_length=length)
            self.assertEqual(ctx.exception.code, code)
        self.assertFalse(list(self.root.glob("*")))


if __name__ == "__main__":
    unittest.main()
