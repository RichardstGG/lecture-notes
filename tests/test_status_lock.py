"""Real process exclusion and lock lifetime; no engines or audio devices."""
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core.status import RunLock, Status
from core.util import write_json


def race_worker(state, work_type, mode, start, finish, results):
    lock = RunLock(state)
    if not start.wait(10):
        return
    conflict = lock.acquire(work_type=work_type, mode=mode)
    results.put((os.getpid(), conflict is None))
    try:
        finish.wait(10)
    finally:
        lock.release()


class AtomicRunLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.locks = []
        self.addCleanup(self.release_all)

    def release_all(self):
        for lock in self.locks:
            lock.release()

    def lock(self):
        lock = RunLock(self.state)
        self.locks.append(lock)
        return lock

    def test_four_work_modes_compete_for_one_lock_across_real_processes(self):
        context = multiprocessing.get_context('spawn')
        start, finish, results = context.Event(), context.Event(), context.Queue()
        processes = [context.Process(target=race_worker,
                     args=(str(self.state), work_type, mode, start, finish, results))
                     for work_type, mode in [('lecture', 'live'), ('lecture', 'summarize'),
                                             ('meeting', 'live'), ('meeting', 'diarize')]]
        try:
            for process in processes:
                process.start()
            start.set()
            rows = [results.get(timeout=15) for _ in processes]
            winners = [pid for pid, won in rows if won]
            self.assertEqual(len(winners), 1, rows)
            self.assertEqual(self.lock().current()['pid'], winners[0])
            self.assertIsNotNone(self.lock().acquire())
        finally:
            finish.set()
            for process in processes:
                if process.pid:
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertIsNone(self.lock().current())
        self.assertIsNone(self.lock().acquire(work_type='meeting', mode='diarize'))

    def test_independent_objects_in_same_process_cannot_overwrite_owner(self):
        owner, contender = self.lock(), self.lock()
        self.assertIsNone(owner.acquire(work_type='lecture', course='original'))
        self.assertIsNotNone(contender.acquire(work_type='meeting'))
        self.assertIsNotNone(owner.acquire(work_type='meeting'))
        contender.update(course='wrong')
        contender.release()
        self.assertEqual(owner.current()['course'], 'original')
        owner.update(pid=99, schema_version=9, session='session')
        self.assertEqual(owner.current()['pid'], os.getpid())
        self.assertEqual(owner.current()['schema_version'], 2)
        owner.release()
        self.assertIsNone(contender.acquire())

    def test_current_and_contender_are_busy_before_metadata_is_published(self):
        owner = self.lock()
        def publish(path, record):
            observer = self.lock()
            self.assertEqual(observer.current()['work_type'], 'unknown')
            self.assertIsNone(observer.current()['pid'])
            self.assertIsNotNone(observer.acquire())
            write_json(path, record)
        with patch('core.status.write_json', side_effect=publish):
            self.assertIsNone(owner.acquire())

    def test_crash_releases_native_lock_and_stale_record_is_reclaimed(self):
        code = '''
import os, sys
from core.status import RunLock
lock = RunLock(sys.argv[1])
assert lock.acquire(work_type="meeting", mode="diarize") is None
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', code, str(self.state)],
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=15)
        self.assertTrue((self.state / 'run.json').exists())
        self.assertIsNone(self.lock().current())
        self.assertIsNone(self.lock().acquire())

    def test_live_legacy_owner_is_respected_and_never_rewritten(self):
        original = json.dumps({'schema_version': 1, 'pid': os.getpid(), 'mode': 'file', 'extra': 5})
        (self.state / 'run.json').write_text(original, encoding='utf-8')
        current = self.lock().acquire(work_type='meeting')
        self.assertEqual(current['work_type'], 'lecture')
        self.assertEqual(current['schema_version'], 1)
        self.assertEqual(current['extra'], 5)
        self.assertEqual((self.state / 'run.json').read_text(encoding='utf-8'), original)

    def test_dead_corrupt_or_wrong_shape_record_does_not_permanently_lock(self):
        for value in ('not json', '[]', 'null', '{"pid":false}', '{"pid":-1}'):
            (self.state / 'run.json').write_text(value, encoding='utf-8')
            lock = self.lock()
            self.assertIsNone(lock.acquire())
            lock.release()

    def test_publication_failure_releases_guard(self):
        with patch('core.status.write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.lock().acquire()
        self.assertIsNone(self.lock().acquire())

    def test_guard_persists_across_releases_and_idle_query_is_read_only(self):
        pristine = self.state / 'not-created'
        self.assertIsNone(RunLock(pristine).current())
        self.assertFalse(pristine.exists())
        lock = self.lock()
        lock.acquire()
        inode = lock.guard_path.stat().st_ino
        lock.release()
        self.assertTrue(lock.guard_path.is_file())
        self.assertIsNone(self.lock().current())
        self.assertIsNone(lock.acquire())
        self.assertEqual(inode, lock.guard_path.stat().st_ino)

    def test_lock_io_failure_is_not_treated_as_idle_or_success(self):
        with patch('core.status._try_lock', side_effect=OSError('unsupported filesystem')):
            with self.assertRaises(OSError):
                self.lock().acquire()
            with self.assertRaises(OSError):
                self.lock().current()

    def test_status_flushes_do_not_publish_out_of_order(self):
        status = Status(self.state, interval=60)
        self.addCleanup(status.close)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def publish(path, record):
            calls.append(record['phase'])
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test release timed out')
            write_json(path, record)
        with patch('core.status.write_json', side_effect=publish):
            first = threading.Thread(target=status.flush)
            second = threading.Thread(target=lambda: status.phase('done'))
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
        result = json.loads((self.state / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(result['phase'], 'done')
        self.assertEqual(calls, ['starting', 'done'])
