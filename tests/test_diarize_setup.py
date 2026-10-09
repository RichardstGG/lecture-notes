"""會議發言者辨識的安裝流程：模型下載驗證、依賴鎖定、upgrade.py 的步驟順序。

全部用暫存資料夾與假下載器，不連網、不需要 sherpa-onnx。
重點是「驗證」這一層：GitHub release 的資產 tag 是可變的，下載到什麼不能直接信任。
"""
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
import setup_engines as SE
import upgrade as U

ROOT = Path(__file__).resolve().parent.parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_tar(path, members):
    """members: {名稱: bytes}"""
    with tarfile.open(path, "w:bz2") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------- 常數的形狀
class PinnedConstantsTests(unittest.TestCase):
    def test_every_model_is_pinned_by_a_full_sha256(self):
        for m in SE.DIARIZE_MODELS:
            self.assertRegex(m["sha256"], r"^[0-9a-f]{64}$", m["name"])
            if "archive_sha256" in m:
                self.assertRegex(m["archive_sha256"], r"^[0-9a-f]{64}$", m["name"])

    def test_downloads_come_from_the_sherpa_onnx_release_page_over_https(self):
        for m in SE.DIARIZE_MODELS:
            self.assertTrue(
                m["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/"),
                m["url"])

    def test_archive_models_name_a_member_and_a_license(self):
        for m in SE.DIARIZE_MODELS:
            if "archive_sha256" in m:
                self.assertIn("member", m)
                self.assertIn("license_member", m)
                self.assertIn("license_file", m)

    def test_filenames_match_the_contract_defaults(self):
        # 契約的 [diarization] 預設路徑；兩邊各自維護就會漂移
        contract = (ROOT / "docs" / "meeting-workbench-contract.md").read_text(encoding="utf-8")
        for m in SE.DIARIZE_MODELS:
            self.assertIn(f"models/{m['file']}", contract, m["file"])

    def test_model_files_are_gitignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(ignore, r"(?m)^models/\s*$")

    def test_requirements_pin_exactly_one_exact_sherpa_version(self):
        lines = [l.strip() for l in
                 (ROOT / "requirements-diarize.txt").read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^sherpa-onnx==\d+\.\d+\.\d+$")

    def test_requirements_and_the_engine_agree_on_the_sherpa_version(self):
        # 兩邊各有一份：pip 裝的是這個檔，doctor 與安裝提示比對的是 SHERPA_ONNX_VERSION
        from core import diarize as D
        text = (ROOT / "requirements-diarize.txt").read_text(encoding="utf-8")
        pinned = re.findall(r"(?m)^\s*sherpa-onnx==(\S+)\s*$", text)
        self.assertEqual(pinned, [D.SHERPA_ONNX_VERSION])


# ---------------------------------------------------------------- 模型下載與驗證
class FetchModelsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.models = self.root / "models"
        self.src = self.root / "src"
        self.src.mkdir()

        self.seg_bytes = b"segmentation-model-bytes"
        self.lic_bytes = b"MIT License\n"
        self.emb_bytes = b"embedding-model-bytes"
        self.member = "pkg/model.onnx"
        self.archive = self.src / "seg.tar.bz2"
        make_tar(self.archive, {self.member: self.seg_bytes, "pkg/LICENSE": self.lic_bytes,
                                "pkg/other.txt": b"noise"})
        (self.src / "emb.onnx").write_bytes(self.emb_bytes)

        self.table = [
            {"name": "segmentation", "license": "MIT", "size_mb": 1, "file": "seg.onnx",
             "url": "https://example.test/seg.tar.bz2",
             "archive_sha256": sha(self.archive.read_bytes()), "member": self.member,
             "sha256": sha(self.seg_bytes), "license_member": "pkg/LICENSE",
             "license_file": "seg.LICENSE.txt"},
            {"name": "embedding", "license": "Apache-2.0", "size_mb": 1, "file": "emb.onnx",
             "url": "https://example.test/emb.onnx", "sha256": sha(self.emb_bytes)},
        ]
        patcher = mock.patch.object(SE, "DIARIZE_MODELS", self.table)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requested = []

    def downloader(self, mapping=None):
        mapping = mapping or {"https://example.test/seg.tar.bz2": self.archive,
                              "https://example.test/emb.onnx": self.src / "emb.onnx"}

        def fake(url, dest):
            self.requested.append(url)
            Path(dest).write_bytes(Path(mapping[url]).read_bytes())
        return fake

    def fetch(self, **kw):
        return SE.fetch_diarization_models(self.models, downloader=kw.pop("dl", None)
                                           or self.downloader(), **kw)

    def leftovers(self):
        return sorted(p.name for p in self.models.iterdir() if p.name.startswith("."))

    def test_fresh_download_extracts_verifies_and_writes_license(self):
        paths = self.fetch()
        self.assertEqual([p.name for p in paths], ["seg.onnx", "emb.onnx"])
        self.assertEqual((self.models / "seg.onnx").read_bytes(), self.seg_bytes)
        self.assertEqual((self.models / "emb.onnx").read_bytes(), self.emb_bytes)
        self.assertEqual((self.models / "seg.LICENSE.txt").read_bytes(), self.lic_bytes)
        self.assertEqual(self.leftovers(), [], "不留 .part／.tmp 半成品")

    def test_only_the_pinned_member_is_extracted(self):
        self.fetch()
        names = sorted(p.name for p in self.models.iterdir())
        self.assertNotIn("other.txt", names)
        self.assertNotIn("model.onnx", names)

    def test_second_run_downloads_nothing(self):
        self.fetch()
        self.requested.clear()
        self.fetch()
        self.assertEqual(self.requested, [])

    def test_existing_file_with_wrong_hash_is_never_overwritten(self):
        self.models.mkdir()
        (self.models / "emb.onnx").write_bytes(b"a model the user swapped in")
        with self.assertRaises(SystemExit):
            self.fetch()
        self.assertEqual((self.models / "emb.onnx").read_bytes(), b"a model the user swapped in")
        self.assertEqual(self.requested.count("https://example.test/emb.onnx"), 0)

    def test_corrupted_plain_download_is_rejected_and_removed(self):
        (self.src / "bad.onnx").write_bytes(b"tampered")
        dl = self.downloader({"https://example.test/seg.tar.bz2": self.archive,
                              "https://example.test/emb.onnx": self.src / "bad.onnx"})
        with self.assertRaises(SystemExit):
            self.fetch(dl=dl)
        self.assertFalse((self.models / "emb.onnx").exists())
        self.assertEqual(self.leftovers(), [])

    def test_archive_with_wrong_hash_is_rejected_before_anything_is_extracted(self):
        other = self.src / "other.tar.bz2"
        make_tar(other, {self.member: b"evil", "pkg/LICENSE": b"x"})
        dl = self.downloader({"https://example.test/seg.tar.bz2": other,
                              "https://example.test/emb.onnx": self.src / "emb.onnx"})
        with self.assertRaises(SystemExit):
            self.fetch(dl=dl)
        self.assertFalse((self.models / "seg.onnx").exists())
        self.assertFalse((self.models / "seg.LICENSE.txt").exists())
        self.assertEqual(self.leftovers(), [])

    def test_member_with_wrong_hash_is_rejected_even_if_archive_hash_matches(self):
        # 釘住的壓縮檔雜湊本身是對的，但裡面的模型與釘的不同（表本身填錯／來源重打包）
        self.table[0]["sha256"] = sha(b"something else")
        with self.assertRaises(SystemExit):
            self.fetch()
        self.assertFalse((self.models / "seg.onnx").exists())
        self.assertEqual(self.leftovers(), [])

    def test_missing_member_is_rejected(self):
        self.table[0]["member"] = "pkg/not-there.onnx"
        with self.assertRaises(SystemExit):
            self.fetch()
        self.assertFalse((self.models / "seg.onnx").exists())
        self.assertEqual(self.leftovers(), [])

    def test_path_traversal_member_names_in_the_archive_are_never_touched(self):
        evil = self.src / "evil.tar.bz2"
        make_tar(evil, {self.member: self.seg_bytes, "pkg/LICENSE": self.lic_bytes,
                        "../escaped.txt": b"pwned"})
        self.table[0]["archive_sha256"] = sha(evil.read_bytes())
        dl = self.downloader({"https://example.test/seg.tar.bz2": evil,
                              "https://example.test/emb.onnx": self.src / "emb.onnx"})
        self.fetch(dl=dl)
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertFalse((self.models.parent / "escaped.txt").exists())

    def test_download_failure_leaves_no_partial_file(self):
        def boom(url, dest):
            Path(dest).write_bytes(b"half a file")
            raise OSError("network dropped")
        with self.assertRaises(OSError):
            self.fetch(dl=boom)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse((self.models / "seg.onnx").exists())

    def test_second_model_failing_keeps_the_first_that_verified(self):
        (self.src / "bad.onnx").write_bytes(b"tampered")
        dl = self.downloader({"https://example.test/seg.tar.bz2": self.archive,
                              "https://example.test/emb.onnx": self.src / "bad.onnx"})
        with self.assertRaises(SystemExit):
            self.fetch(dl=dl)
        self.assertTrue((self.models / "seg.onnx").is_file(), "已驗證過的不必丟掉重來")


class CliTargetTests(unittest.TestCase):
    def run_main(self, argv):
        with mock.patch.object(sys, "argv", ["setup_engines.py", *argv]):
            return SE.main()

    def test_diarize_alone_skips_the_whole_toolchain(self):
        with mock.patch.object(SE, "fetch_diarization_models") as fetch, \
                mock.patch.object(SE, "check_tools") as tools, \
                mock.patch.object(SE, "fetch_repo") as repo, \
                mock.patch.object(SE, "build") as build:
            code = self.run_main(["diarize"])
        self.assertEqual(code, 0)
        fetch.assert_called_once_with()
        tools.assert_not_called()
        repo.assert_not_called()
        build.assert_not_called()

    def test_default_run_does_not_download_the_diarization_models(self):
        # 預設（不指定目標）只編 whisper 與 llama；會議模型只在明確要求或 upgrade.py 時才取
        with mock.patch.object(SE, "fetch_diarization_models") as fetch, \
                mock.patch.object(SE, "check_tools", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                self.run_main([])
        fetch.assert_not_called()

    def test_unknown_target_message_lists_diarize(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.run_main(["bogus"])
        self.assertIn("diarize", err.getvalue())


# ---------------------------------------------------------------- upgrade.py
class UpgradeDiarizationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "requirements-diarize.txt").write_text("sherpa-onnx==0\n", encoding="utf-8")
        patcher = mock.patch.object(U, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def completed(self):
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def test_installs_into_the_project_venv_then_fetches_models(self):
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        calls = []
        with mock.patch.object(U, "run", side_effect=lambda c, **k: calls.append(c) or self.completed()):
            U.install_diarization()
        self.assertEqual(calls, [
            [python, "-m", "pip", "install", "-r", self.root / "requirements-diarize.txt"],
            [sys.executable, self.root / "setup_engines.py", "diarize"],
        ])

    def test_creates_the_venv_first_when_missing(self):
        calls = []
        with mock.patch.object(U, "run", side_effect=lambda c, **k: calls.append(c) or self.completed()):
            U.install_diarization()
        self.assertEqual(calls[0], [sys.executable, "-m", "venv", self.root / ".venv"])
        self.assertEqual(len(calls), 3)

    def test_pip_failure_stops_before_downloading_models(self):
        def run(command, **kwargs):
            if "pip" in [str(c) for c in command]:
                raise U.UpgradeError("pip 失敗")
            return self.completed()
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").write_text("", encoding="utf-8")
        with mock.patch.object(U, "run", side_effect=run) as r, \
                self.assertRaises(U.UpgradeError):
            U.install_diarization()
        self.assertEqual(r.call_count, 1)

    def test_default_upgrade_installs_diarization_before_the_ui(self):
        order = []
        args = U.parse_args(["--skip-pull", "--skip-engines"])
        with mock.patch.object(U, "install_diarization", side_effect=lambda: order.append("diarize")), \
                mock.patch.object(U, "install_ui", side_effect=lambda: order.append("ui")):
            U.perform_upgrade(args)
        self.assertEqual(order, ["diarize", "ui"])

    def test_skip_flag_skips_only_diarization(self):
        order = []
        args = U.parse_args(["--skip-pull", "--skip-engines", "--skip-diarization"])
        with mock.patch.object(U, "install_diarization", side_effect=lambda: order.append("diarize")), \
                mock.patch.object(U, "install_ui", side_effect=lambda: order.append("ui")):
            U.perform_upgrade(args)
        self.assertEqual(order, ["ui"])

    def test_skip_ui_still_installs_diarization(self):
        order = []
        args = U.parse_args(["--skip-pull", "--skip-engines", "--skip-ui"])
        with mock.patch.object(U, "install_diarization", side_effect=lambda: order.append("diarize")), \
                mock.patch.object(U, "install_ui", side_effect=lambda: order.append("ui")):
            U.perform_upgrade(args)
        self.assertEqual(order, ["diarize"])

    def test_engines_are_updated_before_diarization(self):
        order = []
        args = U.parse_args(["--skip-pull", "--skip-ui"])
        with mock.patch.object(U, "ensure_no_active_run"), \
                mock.patch.object(U, "run", side_effect=lambda c, **k: order.append("engines") or self.completed()), \
                mock.patch.object(U, "install_diarization", side_effect=lambda: order.append("diarize")):
            U.perform_upgrade(args)
        self.assertEqual(order, ["engines", "diarize"])


if __name__ == "__main__":
    unittest.main()
