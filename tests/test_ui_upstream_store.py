"""Private upstream settings store and redacted public projection."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests import _pathfix  # noqa: F401
from ui.backend.upstream_store import UpstreamStore, UpstreamStoreError


class UpstreamStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "upstreams.toml"
        self.store = UpstreamStore(self.path)
        self.secret = "private-secret-token"

    def payload(self, **changes):
        data = {
            "name": "Lab GPU", "base_url": "http://192.0.2.10:8000/v1",
            "model": "private-model", "auth_mode": "api_key", "api_key": self.secret,
        }
        data.update(changes)
        return data

    def test_create_list_update_delete_never_returns_connection_details(self):
        created = self.store.create("lab", self.payload())
        self.assertEqual(created, {
            "id": "lab", "name": "Lab GPU", "kind": "api", "auth_mode": "api_key",
        })
        public = self.store.list()
        self.assertEqual(public["upstreams"], [created])
        self.assertNotIn(self.secret, repr(public))
        self.assertNotIn("192.0.2.10", repr(public))
        self.assertNotIn("private-model", repr(public))
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["upstreams"]["lab"]["api_key"], self.secret)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

        updated = self.store.update("lab", self.payload(
            name="New name", auth_mode="environment", api_key=None,
            api_key_env="LEC_LAB_KEY",
        ))
        self.assertEqual(updated["auth_mode"], "environment")
        parsed = tomllib.loads(self.path.read_text(encoding="utf-8"))["upstreams"]["lab"]
        self.assertNotIn("api_key", parsed)
        self.assertEqual(parsed["api_key_env"], "LEC_LAB_KEY")

        self.assertEqual(self.store.delete("lab")["deleted"], "lab")
        self.assertEqual(self.store.list()["upstreams"], [])
        self.assertEqual(tomllib.loads(self.path.read_text(encoding="utf-8"))["upstreams"], {})

    def test_invalid_values_and_existing_ids_do_not_overwrite(self):
        self.store.create("lab", self.payload())
        for changes in (
            {"base_url": "http://user:secret@example.invalid/v1"},
            {"base_url": "https://example.invalid/v1?key=secret"},
            {"base_url": "file:///private"},
            {"auth_mode": "environment", "api_key": None, "api_key_env": "BAD-NAME"},
            {"auth_mode": "api_key", "api_key": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(UpstreamStoreError) as error:
                self.store.update("lab", self.payload(**changes))
            self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(UpstreamStoreError) as error:
            self.store.create("lab", self.payload(name="duplicate"))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.list()["upstreams"][0]["name"], "Lab GPU")

    def test_malformed_or_symlinked_store_is_rejected_without_echoing_content(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[upstreams.lab]\napi_key = "private-secret')
        with self.assertRaises(UpstreamStoreError) as error:
            self.store.list()
        self.assertNotIn("private-secret", str(error.exception))
        self.path.unlink()
        target = Path(self.tmp.name) / "target.toml"
        target.write_text("private-secret")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(UpstreamStoreError):
            self.store.list()
        self.assertEqual(target.read_text(), "private-secret")


if __name__ == "__main__":
    unittest.main()
