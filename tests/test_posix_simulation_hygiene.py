"""POSIX 模擬測試的衛生檢查：強制走 POSIX 分支時，不可以讓真的 os.kill／os.killpg 被呼叫。

為什麼需要：這類錯誤在 Linux 上完全無害，在 Windows 上卻能殺掉整個測試 runner。Windows 的
os.kill(pid, sig) 不是 POSIX 的語意：sig 0／1 是 CTRL_C_EVENT／CTRL_BREAK_EVENT（對整個主控台的行程
送事件，包含 runner 自己），其他 sig 是 TerminateProcess。一個把 IS_WINDOWS 設成 False 卻沒 mock
os.kill 的測試（tests/test_platform_process.py 的 test_posix_current_process_is_alive）就是這樣
讓 Windows basic 以 0xC000013A 結束、沒有產出任何測試結果（診斷見 docs/platform-windows-testing.md）。

這是靜態檢查（找文字模式），不是證明；它抓得到「強制 POSIX 分支＋呼叫行程工具＋沒有 mock kill」這個
已知的組合，抓不到所有可能讓測試對自己送訊號的寫法。
"""
import ast
import re
import unittest
from pathlib import Path

from . import _pathfix  # noqa: F401

TESTS = Path(__file__).resolve().parent
FORCES_POSIX = re.compile(r'''IS_WINDOWS["']?\s*,\s*False''')
TOUCHES_PROCESSES = re.compile(r"\b(pid_alive|kill_tree|kill_now|interrupt)\(")
MOCKS_KILL = re.compile(r'''patch(?:\.object)?\([^)]*["'](?:[\w.]*\.)?kill(?:pg)?["']''')


def unsafe_posix_simulations(source):
    """回傳原始碼中不安全的測試方法名稱。"""
    tree = ast.parse(source)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test"):
            continue
        seg = ast.get_source_segment(source, fn) or ""
        if (FORCES_POSIX.search(seg) and TOUCHES_PROCESSES.search(seg)
                and not MOCKS_KILL.search(seg)):
            bad.append(fn.name)
    return bad


class CheckerTests(unittest.TestCase):
    """檢查器本身要先證明抓得到、也不會亂報。"""

    DANGEROUS = '''
class T:
    def test_alive(self):
        with mock.patch.object(P, "IS_WINDOWS", False):
            self.assertTrue(P.pid_alive(os.getpid()))
'''
    SAFE_MOCKED = '''
class T:
    def test_alive(self):
        with mock.patch.object(P, "IS_WINDOWS", False), mock.patch.object(P.os, "kill") as kill:
            self.assertTrue(P.pid_alive(os.getpid()))
'''
    SAFE_KILLPG = '''
class T:
    def test_now(self):
        with mock.patch.object(P, "IS_WINDOWS", False), \\
                mock.patch.object(P.os, "killpg", create=True) as killpg:
            P.kill_now(123)
'''
    NO_PROCESS_TOOLS = '''
class T:
    def test_layout(self):
        with mock.patch.object(P, "IS_WINDOWS", False):
            self.assertEqual(P.find_engine_bin("x", "y"), "z")
'''
    WINDOWS_BRANCH = '''
class T:
    def test_windows_alive(self):
        with mock.patch.object(P, "IS_WINDOWS", True):
            P.pid_alive(123)
'''

    def test_flags_the_known_dangerous_combination(self):
        self.assertEqual(unsafe_posix_simulations(self.DANGEROUS), ["test_alive"])

    def test_accepts_tests_that_mock_the_real_kill(self):
        self.assertEqual(unsafe_posix_simulations(self.SAFE_MOCKED), [])
        self.assertEqual(unsafe_posix_simulations(self.SAFE_KILLPG), [])

    def test_ignores_posix_simulations_that_never_touch_processes(self):
        self.assertEqual(unsafe_posix_simulations(self.NO_PROCESS_TOOLS), [])

    def test_ignores_tests_that_simulate_the_windows_branch(self):
        self.assertEqual(unsafe_posix_simulations(self.WINDOWS_BRANCH), [])


class RepositoryTests(unittest.TestCase):
    def test_no_test_forces_the_posix_branch_without_mocking_kill(self):
        offenders = {}
        for path in sorted(TESTS.glob("test_*.py")):
            bad = unsafe_posix_simulations(path.read_text(encoding="utf-8"))
            if bad:
                offenders[path.name] = bad
        self.assertEqual(
            offenders, {},
            "這些測試強制走 POSIX 分支並呼叫行程工具，卻沒有 mock os.kill／os.killpg。"
            "Windows 上真的呼叫會對測試 runner 自己送 CTRL_C_EVENT 而中斷整個測試。"
            "請像同檔案其他測試那樣 mock P.os.kill，並斷言呼叫的參數。")


if __name__ == "__main__":
    unittest.main()
