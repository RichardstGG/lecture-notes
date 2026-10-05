"""core/servers.py 的擁有權紀錄位置與 ensure() 的沿用／重啟判斷。

紀錄位置是個 contract：`lec doctor` 的「狀態資料夾」與 README 都說狀態檔在
`cfg.state_dir()`。舊版 `servers.py` 用 `cfg.path(cfg["paths"]["state_dir"])`，
而預設值是字面字串 "auto"，所以紀錄其實落在 repo 內的 `<app_root>/auto/servers/`，
每次執行都在工作樹裡產生未追蹤檔案。下面前兩組測試把正確位置釘住。

全部是 mock-based，不會真的啟動 whisper-server／llama-server。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _pathfix  # noqa: F401
from core import config as C
from core import servers as S


class _Cfg:
    """只提供 ManagedServer 子類別會用到的那幾個介面。"""

    def __init__(self, state_dir_value, app_root):
        self.data = {"paths": {"state_dir": state_dir_value,
                               "whisper_dir": "whisper.cpp", "llama_dir": "llama.cpp"},
                     "whisper": {"host": "127.0.0.1", "port": 8178,
                                 "language": "zh", "threads": 8},
                     "llm": {"host": "127.0.0.1", "port": 8179, "ngl": 99, "ctx": 8192,
                             "parallel": 1, "startup_timeout": 300, "extra_args": []}}
        self.app_root = Path(app_root)
        self.resolved_state_dir = self.app_root / "resolved-state"

    def __getitem__(self, section):
        return self.data[section]

    def path(self, value):
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.app_root / p)

    def state_dir(self):
        return self.resolved_state_dir

    def whisper_model_path(self):
        return str(self.app_root / "models" / "whisper.bin")

    def llm_model(self, name=None):
        return {"name": "qwen3-8b", "path": str(self.app_root / "models" / "q.gguf"),
                "disable_thinking": True}


class RecordLocationTests(unittest.TestCase):
    """紀錄必須寫在 cfg.state_dir() 之下，不是 cfg.path(paths.state_dir)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _Cfg("auto", self.tmp.name)

    def test_whisper_record_lives_under_state_dir(self):
        srv = S.WhisperServer(self.cfg)
        self.assertEqual(srv.own_file,
                         self.cfg.state_dir() / "servers" / "whisper-server.json")

    def test_llama_record_lives_under_state_dir(self):
        srv = S.LlamaServer(self.cfg)
        self.assertEqual(srv.own_file,
                         self.cfg.state_dir() / "servers" / "llama-server.json")

    def test_record_is_not_written_into_the_repo_for_the_auto_default(self):
        # 迴歸測試：預設 paths.state_dir = "auto" 曾被當成相對路徑，
        # 讓紀錄掉進 <app_root>/auto/servers/，在工作樹留下未追蹤檔案。
        for srv in (S.WhisperServer(self.cfg), S.LlamaServer(self.cfg)):
            with self.subTest(srv.name):
                self.assertNotIn("auto", srv.own_file.parts)
                self.assertFalse(
                    str(srv.own_file).startswith(str(self.cfg.app_root / "auto")))

    def test_absolute_state_dir_leaves_nothing_to_migrate(self):
        absolute = Path(self.tmp.name) / "explicit-state"
        cfg = _Cfg(str(absolute), self.tmp.name)
        cfg.resolved_state_dir = absolute
        srv = S.WhisperServer(cfg)
        self.assertEqual(srv.own_file, absolute / "servers" / "whisper-server.json")
        self.assertEqual(srv.legacy_own_file, srv.own_file)

    def test_real_config_puts_records_beside_run_json(self):
        # 用真正的 Config 走一遍：紀錄與 run.json 必須在同一個狀態資料夾。
        cfg, _ = C.load(None)
        for srv in (S.WhisperServer(cfg), S.LlamaServer(cfg)):
            with self.subTest(srv.name):
                self.assertEqual(srv.own_file.parent.parent, cfg.state_dir())


class LegacyRecordAdoptionTests(unittest.TestCase):
    """升級時把舊位置還有效的紀錄接過來，不要把執行中的 server 變成孤兒。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _Cfg("auto", self.tmp.name)
        self.srv = S.WhisperServer(self.cfg)
        self.record = {"pid": 4242, "port": 8178, "model": "/models/a.bin"}

    def _write_legacy(self, data=None):
        p = self.srv.legacy_own_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data if data is not None else self.record),
                     encoding="utf-8")
        return p

    def test_legacy_path_is_the_old_buggy_location(self):
        self.assertEqual(self.srv.legacy_own_file,
                         self.cfg.app_root / "auto" / "servers" / "whisper-server.json")

    def test_adopts_the_legacy_record_and_removes_the_old_file(self):
        legacy = self._write_legacy()
        self.srv._adopt_legacy_record()
        self.assertEqual(S.read_json(self.srv.own_file), self.record)
        self.assertFalse(legacy.exists())

    def test_existing_record_wins_and_legacy_is_cleaned_up(self):
        self.srv.own_file.parent.mkdir(parents=True, exist_ok=True)
        current = {"pid": 11, "port": 8178, "model": "/models/current.bin"}
        S.write_json(self.srv.own_file, current)
        self._write_legacy()
        self.srv._adopt_legacy_record()
        self.assertEqual(S.read_json(self.srv.own_file), current)

    def test_no_legacy_file_is_a_noop(self):
        self.srv._adopt_legacy_record()
        self.assertFalse(self.srv.own_file.exists())

    def test_unreadable_legacy_record_is_discarded_not_adopted(self):
        legacy = self._write_legacy()
        legacy.write_text("not json at all", encoding="utf-8")
        self.srv._adopt_legacy_record()
        self.assertFalse(self.srv.own_file.exists())
        self.assertFalse(legacy.exists())

    def test_adoption_failure_is_not_fatal(self):
        self._write_legacy()
        with mock.patch.object(S, "write_json", side_effect=OSError("read-only")):
            self.srv._adopt_legacy_record()        # 不可以丟例外
        self.assertFalse(self.srv.own_file.exists())

    def test_ensure_adopts_before_reading_the_record(self):
        # 舊紀錄指向同一個模型，所以接管成功後應該「沿用」而不是重啟。
        self._write_legacy({"pid": 4242, "port": 8178, "model": self.srv.model_path})
        srv = _stub(self.srv, responding=True, model=self.srv.model_path)
        with mock.patch.object(S, "pid_alive", return_value=True):
            outcome = srv.ensure(Path(self.tmp.name) / "x.log")
        self.assertEqual(outcome, "reused")
        self.assertEqual(srv.pid, 4242)
        self.assertTrue(srv.owned, "接管後這台 server 要由我們負責關閉")
        self.assertFalse(srv.legacy_own_file.exists())


def _stub(srv, responding, model, ready=True):
    """把 ensure() 會碰到的外部行為換成固定值。"""
    srv.responding = lambda: responding
    srv.is_ready = lambda: ready
    srv.running_model = lambda own: model
    srv._wait_ready = lambda proc=None: None
    srv.started = []
    srv.killed = []
    srv._start = lambda: srv.started.append(True)
    srv._kill = lambda pid, timeout=15: srv.killed.append(pid)
    return srv


class EnsureDecisionTests(unittest.TestCase):
    """ensure() 的沿用／重啟／外部 server 判斷（原本完全沒有測試）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.log = Path(self.tmp.name) / "server.log"

    def _server(self, model="/models/a.bin"):
        return S.ManagedServer("127.0.0.1", 18178, model, self.state)

    def _own(self, srv, pid=777, port=18178, model="/models/a.bin"):
        srv.own_file.parent.mkdir(parents=True, exist_ok=True)
        S.write_json(srv.own_file, {"pid": pid, "port": port, "model": model})

    def test_reuses_our_own_server_with_the_same_model(self):
        srv = _stub(self._server(), responding=True, model="/models/a.bin")
        self._own(srv)
        with mock.patch.object(S, "pid_alive", return_value=True):
            self.assertEqual(srv.ensure(self.log), "reused")
        self.assertEqual(srv.started, [])
        self.assertTrue(srv.owned)
        self.assertEqual(srv.pid, 777)

    def test_restarts_our_own_server_when_the_model_differs(self):
        srv = _stub(self._server("/models/b.bin"), responding=True, model="/models/a.bin")
        self._own(srv)
        with mock.patch.object(S, "pid_alive", return_value=True):
            self.assertEqual(srv.ensure(self.log), "started")
        self.assertEqual(srv.killed, [777])
        self.assertEqual(srv.started, [True])

    def test_stale_record_is_removed_and_a_fresh_server_starts(self):
        srv = _stub(self._server(), responding=False, model=None)
        self._own(srv)
        with mock.patch.object(S, "pid_alive", return_value=False):
            self.assertEqual(srv.ensure(self.log), "started")
        self.assertFalse(srv.own_file.exists())
        self.assertEqual(srv.started, [True])

    def test_record_for_another_port_is_discarded(self):
        srv = _stub(self._server(), responding=False, model=None)
        self._own(srv, port=19999)
        with mock.patch.object(S, "pid_alive", return_value=True):
            self.assertEqual(srv.ensure(self.log), "started")
        self.assertFalse(srv.own_file.exists())

    def test_external_server_with_the_same_model_is_reused_but_not_owned(self):
        srv = _stub(self._server(), responding=True, model="/models/a.bin")
        self.assertEqual(srv.ensure(self.log), "external")
        self.assertFalse(srv.owned)
        self.assertEqual(srv.started, [])

    def test_external_server_with_an_unknown_model_is_reused_but_not_owned(self):
        srv = _stub(self._server(), responding=True, model=None)
        self.assertEqual(srv.ensure(self.log), "external")
        self.assertFalse(srv.owned)

    def test_external_server_with_a_different_model_raises(self):
        srv = _stub(self._server("/models/b.bin"), responding=True, model="/models/a.bin")
        with self.assertRaises(S.ServerError) as caught:
            srv.ensure(self.log)
        self.assertIn("/models/a.bin", str(caught.exception))
        self.assertEqual(srv.killed, [], "不可以去動別人開的 server")

    def test_nothing_running_starts_a_server(self):
        srv = _stub(self._server(), responding=False, model=None)
        self.assertEqual(srv.ensure(self.log), "started")
        self.assertEqual(srv.started, [True])


class StopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = S.ManagedServer("127.0.0.1", 18178, "/models/a.bin",
                                   Path(self.tmp.name) / "state")
        self.srv.own_file.parent.mkdir(parents=True, exist_ok=True)
        S.write_json(self.srv.own_file, {"pid": 5, "port": 18178, "model": "/models/a.bin"})

    def test_stop_removes_the_record_only_for_servers_we_own(self):
        self.srv.pid, self.srv.owned = 5, True
        with mock.patch.object(S.P, "kill_tree") as kill:
            self.srv.stop()
        kill.assert_called_once()
        self.assertFalse(self.srv.own_file.exists())
        self.assertFalse(self.srv.owned)

    def test_stop_leaves_an_external_server_and_its_record_alone(self):
        self.srv.pid, self.srv.owned = 5, False
        with mock.patch.object(S.P, "kill_tree") as kill:
            self.srv.stop()
        kill.assert_not_called()
        self.assertTrue(self.srv.own_file.exists())


if __name__ == "__main__":
    unittest.main()
