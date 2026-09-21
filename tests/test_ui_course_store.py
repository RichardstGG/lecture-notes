"""Filesystem safety tests for UI course configuration editing."""
import tempfile
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.backend.cli_client import LecCommandError
from ui.backend.course_store import CourseStore, CourseStoreError


class StubClient:
    def __init__(self, root):
        self.root = root

    async def create_course(self, course_id):
        (self.root / f"{course_id}.toml").write_text(
            f'[course]\nname = "{course_id}"\n', encoding="utf-8",
        )

    async def validate_course(self, path):
        if "unknown-model" in Path(path).read_text(encoding="utf-8"):
            raise LecCommandError("cli_failed", "model is not defined", exit_code=1)


class CourseStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "courses"
        self.root.mkdir()
        self.store = CourseStore(self.root)
        self.client = StubClient(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_create_and_read_unicode_course(self):
        detail = await self.store.create(self.client, "資料結構")
        self.assertEqual(detail["id"], "資料結構")
        self.assertEqual(detail["api_version"], 1)
        self.assertIn("資料結構", detail["content"])

    async def test_rejects_nonportable_ids_and_duplicate_files(self):
        for course_id in (
            "../escape", "nested/path", "-option", ".hidden", "NUL", "課" * 100,
        ):
            with self.subTest(course_id=course_id), self.assertRaises(CourseStoreError):
                await self.store.create(self.client, course_id)
        (self.root / "existing.toml").write_text("[course]\n", encoding="utf-8")
        with self.assertRaises(CourseStoreError) as ctx:
            await self.store.create(self.client, "existing")
        self.assertEqual(ctx.exception.code, "course_exists")

    async def test_rejects_symlinked_course(self):
        outside = Path(self.tmp.name) / "outside.toml"
        outside.write_text("[course]\n", encoding="utf-8")
        link = self.root / "linked.toml"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(CourseStoreError) as ctx:
            self.store.get("linked")
        self.assertEqual(ctx.exception.code, "course_not_found")

    async def test_invalid_toml_and_cli_validation_leave_original_unchanged(self):
        target = self.root / "course.toml"
        original = '[course]\nname = "original"\n'
        target.write_text(original, encoding="utf-8")
        original_mode = target.stat().st_mode

        for content in ('[course\n', '[summary]\nmodel = "unknown-model"\n'):
            with self.subTest(content=content), self.assertRaises(CourseStoreError):
                await self.store.update(self.client, "course", content)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

        updated = '[course]\nname = "updated"\n'
        detail = await self.store.update(self.client, "course", updated)
        self.assertEqual(detail["content"], updated)
        self.assertEqual(target.read_text(encoding="utf-8"), updated)
        self.assertEqual(target.stat().st_mode, original_mode)
        self.assertFalse((self.root / "course.toml.tmp").exists())

    async def test_content_size_limit_applies_to_reads_and_updates(self):
        target = self.root / "course.toml"
        target.write_text("[course]\n", encoding="utf-8")
        store = CourseStore(self.root, max_bytes=20)
        with self.assertRaises(CourseStoreError) as ctx:
            await store.update(self.client, "course", "[course]\nname = \"too long\"\n")
        self.assertEqual(ctx.exception.code, "course_too_large")

        target.write_text("x" * 21, encoding="utf-8")
        with self.assertRaises(CourseStoreError) as ctx:
            store.get("course")
        self.assertEqual(ctx.exception.code, "course_too_large")


if __name__ == "__main__":
    unittest.main()
