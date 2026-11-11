"""Tests for doit.dependency — migrated from pytest to unittest."""
import os
import sys
import time
import tempfile
import shutil
from dbm import whichdb
from sys import executable
from unittest.mock import patch
import unittest

from doit.task import Task
from doit.dependency import get_md5, get_file_md5, get_file_sha256
from doit.dependency import DbmDB, JsonDB, SqliteDB, Dependency
from doit.dependency import DatabaseException, UptodateCalculator
from doit.dependency import (
    FileChangedChecker, MD5Checker, SHA256Checker, TimestampChecker)
from doit.dependency import DependencyStatus
from tests.support import get_abspath, backend_map, db_ext
from tests.support import DependencyFileMixin

# path to test folder (the original tests/ dir, where sample files live)
TEST_PATH = os.path.join(os.path.dirname(__file__), '..', 'tests')
PROGRAM = "%s %s/sample_process.py" % (executable, TEST_PATH)


####
# dependencies are files only (not other tasks).
#
# whenever a task has a dependency the runner checks if this dependency
# was modified since last successful run. if not the task is skipped.

# since more than one task might have the same dependency, and the tasks
# might have different results (success/failure). the signature is associated
# not only with the file, but also with the task.
#
# save in db (task - dependency - (timestamp, size, signature))
# taskId_dependency => signature(dependency)
# taskId is md5(CmdTask.task)


# ---------------------------------------------------------------------------
# Bare test functions wrapped in a TestCase
# ---------------------------------------------------------------------------

class TestUtilFunctions(unittest.TestCase):

    def test_unicode_md5(self):
        data = "\u6211"
        # no exception is raised
        self.assertTrue(get_md5(data))

    def test_md5(self):
        filePath = os.path.join(TEST_PATH, "sample_md5.txt")
        # result got using command line md5sum, with different line-endings
        # to deal with different GIT configurations:
        expected_lf = "45d1503cb985898ab5bd8e58973007dd"
        expected_crlf = "cf7b48b2fec3b581b135f7c9a1f7ae04"
        self.assertIn(get_file_md5(filePath), {expected_lf, expected_crlf})

    def test_sqlite_import(self):
        """Checks that SQLite module is not imported until the SQLite class is instantiated"""
        from doit import dependency
        self.assertFalse(hasattr(dependency, 'sqlite3'))


# ---------------------------------------------------------------------------
# DependencyTestBase mixin: parametrized dep_manager by backend_name
# ---------------------------------------------------------------------------

class DependencyTestBase:
    """Mixin providing self.dep_manager for the backend given by backend_name."""
    backend_name = None

    def setUp(self):
        super().setUp()
        self._dep_tmpdir = tempfile.mkdtemp(prefix='doit-test-dep-')
        filename = os.path.join(self._dep_tmpdir, 'testdb')
        dep_class = backend_map[self.backend_name]
        try:
            if self.backend_name.startswith('dbm.'):
                self.dep_manager = Dependency(
                    dep_class, filename, module_name=self.backend_name)
            else:
                self.dep_manager = Dependency(dep_class, filename)
        except ImportError:
            self.skipTest(f'"{self.backend_name}" not available.')
        if self.backend_name == 'dbm':
            self.dep_manager.whichdb = whichdb(self.dep_manager.name)
        else:
            self.dep_manager.whichdb = self.backend_name
        self.dep_manager.name_ext = db_ext.get(
            self.dep_manager.whichdb, [''])

    def tearDown(self):
        if hasattr(self, 'dep_manager') and not self.dep_manager._closed:
            self.dep_manager.close()
        if hasattr(self, '_dep_tmpdir'):
            shutil.rmtree(self._dep_tmpdir, ignore_errors=True)
        super().tearDown()


# FIXME there was major refactor breaking classes from dependency,
# unit-tests could be more specific to base classes.

# ---------------------------------------------------------------------------
# TestDependencyDb
# ---------------------------------------------------------------------------

class _DependencyDbTests:
    """Tests for basic DB operations (get/set/dump/remove etc.)."""

    # adding a new value to the DB
    def test_get_set(self):
        self.dep_manager._set("taskId_X", "dependency_A", "da_md5")
        value = self.dep_manager._get("taskId_X", "dependency_A")
        self.assertEqual("da_md5", value)

    def test_get_set_unicode_name(self):
        self.dep_manager._set("taskId_\u6211", "dependency_A", "da_md5")
        value = self.dep_manager._get("taskId_\u6211", "dependency_A")
        self.assertEqual("da_md5", value)

    #
    def test_dump(self):
        # save and close db
        self.dep_manager._set("taskId_X", "dependency_A", "da_md5")
        self.dep_manager.close()

        # open it again and check the value
        d2 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        value = d2._get("taskId_X", "dependency_A")
        self.assertEqual("da_md5", value)
        d2.close()

    def test_corrupted_file(self):
        if self.dep_manager.whichdb == 'sqlite3':
            self.skipTest('close() does not release fp on windows')
        if self.dep_manager.whichdb == 'dbm.ndbm':
            # TODO: ndbm raises no Exception, but it writes an error on STDERR.
            self.skipTest('dbm.ndbm does not raise Exception')
        if self.dep_manager.whichdb == 'dbm.dumb' and sys.version_info >= (3, 13):
            # Python 3.13 dbm.dumb silently recovers from corrupted files
            self.skipTest('dbm.dumb does not raise Exception on 3.13+')

        # create some corrupted files
        for name_ext in self.dep_manager.name_ext:
            full_name = self.dep_manager.name + name_ext
            fd = open(full_name, 'w')
            fd.seek(0)
            fd.write("""{"x": y}""")
            fd.close()
        self.assertRaises(
            DatabaseException, Dependency,
            self.dep_manager.db_class, self.dep_manager.name)

    def test_corrupted_file_unrecognized_excep(self):
        if self.dep_manager.whichdb == 'sqlite3':
            self.skipTest('close() does not release fp on windows')
        if self.dep_manager.whichdb == 'dbm.ndbm':
            # TODO: ndbm raises no Exception, but it writes an error on STDERR.
            self.skipTest('dbm.ndbm does not raise Exception')
        if self.dep_manager.whichdb == 'dbm.dumb' and sys.version_info >= (3, 13):
            self.skipTest('dbm.dumb does not raise Exception on 3.13+')

        # create some corrupted files
        for name_ext in self.dep_manager.name_ext:
            full_name = self.dep_manager.name + name_ext
            fd = open(full_name, 'w')
            fd.seek(0)
            fd.write("""{"x": y}""")
            fd.close()
        with patch.object(DbmDB, 'DBM_CONTENT_ERROR_MSG', 'xxx'):
            self.assertRaises(
                DatabaseException, Dependency,
                self.dep_manager.db_class, self.dep_manager.name)

    # _get must return None if entry doesnt exist.
    def test_getNonExistent(self):
        self.assertIsNone(self.dep_manager._get("taskId_X", "dependency_A"))

    def test_in(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.assertTrue(self.dep_manager._in("taskId_ZZZ"))
        self.assertFalse(self.dep_manager._in("taskId_hohoho"))

    def test_remove(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.dep_manager._set("taskId_ZZZ", "dep_2", "13")
        self.dep_manager._set("taskId_YYY", "dep_1", "14")
        self.dep_manager.remove("taskId_ZZZ")
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_1"))
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_2"))
        self.assertEqual("14", self.dep_manager._get("taskId_YYY", "dep_1"))

    # special test for DBM backend and "dirty"/caching mechanism
    def test_remove_from_non_empty_file(self):
        # 1 - put 2 tasks of file
        self.dep_manager._set("taskId_XXX", "dep_1", "x")
        self.dep_manager._set("taskId_YYY", "dep_1", "x")
        self.dep_manager.close()
        # 2 - re-open and remove one task
        reopened = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        reopened.remove("taskId_YYY")
        reopened.close()
        # 3 - re-open again and check task was really removed
        reopened2 = Dependency(self.dep_manager.db_class, self.dep_manager.name)
        self.assertTrue(reopened2._in("taskId_XXX"))
        self.assertFalse(reopened2._in("taskId_YYY"))
        reopened2.close()

    def test_remove_all(self):
        self.dep_manager._set("taskId_ZZZ", "dep_1", "12")
        self.dep_manager._set("taskId_ZZZ", "dep_2", "13")
        self.dep_manager._set("taskId_YYY", "dep_1", "14")
        self.dep_manager.remove_all()
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_1"))
        self.assertIsNone(self.dep_manager._get("taskId_ZZZ", "dep_2"))
        self.assertIsNone(self.dep_manager._get("taskId_YYY", "dep_1"))


class TestDependencyDbJson(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'json'

class TestDependencyDbSqlite(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestDependencyDbDbmGnu(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestDependencyDbDbmNdbm(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestDependencyDbDbmDumb(DependencyTestBase, _DependencyDbTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestSaveSuccess
# ---------------------------------------------------------------------------

class _SaveSuccessTests:
    """Tests for Dependency.save_success."""

    def test_save_result(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1)
        self.assertEqual(get_md5("result"), self.dep_manager._get(t1.name, "result:"))
        self.assertEqual(get_md5("result"), self.dep_manager.get_result(t1.name))

    def test_save_result_hash(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1, result_hash='abc')
        self.assertEqual('abc', self.dep_manager._get(t1.name, "result:"))

    def test_save_resultNone(self):
        t1 = Task('t_name', None)
        self.dep_manager.save_success(t1)
        self.assertIsNone(self.dep_manager._get(t1.name, "result:"))

    def test_save_result_dict(self):
        t1 = Task('t_name', None)
        t1.result = {'d': "result"}
        self.dep_manager.save_success(t1)
        self.assertEqual({'d': "result"}, self.dep_manager._get(t1.name, "result:"))

    def test_save_file_md5(self):
        # create a test dependency file
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("i am the first dependency ever for doit")
        ff.close()

        # save it
        t1 = Task("taskId_X", None, [filePath])
        self.dep_manager.save_success(t1)
        expected = "a1bb792202ce163b4f0d17cb264c04e1"
        value = self.dep_manager._get("taskId_X", filePath)
        self.assertEqual(os.path.getmtime(filePath), value[0])  # timestamp
        self.assertEqual(39, value[1])  # size
        self.assertEqual(expected, value[2])  # MD5

    def test_save_skip(self):
        filePath = get_abspath("data/dependency1")
        # ensure file exists
        ff = open(filePath, "w")
        ff.write("content")
        ff.close()
        t1 = Task("taskId_X", None, [filePath])
        self.dep_manager._set(t1.name, filePath, (345, 0, "fake"))
        with patch.object(os.path, 'getmtime', lambda x: 345):
            # save but md5 is not modified
            self.dep_manager.save_success(t1)
        got = self.dep_manager._get("taskId_X", filePath)
        self.assertEqual("fake", got[2])

    def test_save_files(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()
        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part2")
        ff.close()
        self.assertIsNone(self.dep_manager._get("taskId_X", filePath))
        self.assertIsNone(self.dep_manager._get("taskId_X", filePath2))

        t1 = Task("taskId_X", None, [filePath, filePath2])
        self.dep_manager.save_success(t1)
        self.assertIsNotNone(self.dep_manager._get("taskId_X", filePath))
        self.assertIsNotNone(self.dep_manager._get("taskId_X", filePath2))
        self.assertEqual(
            set(self.dep_manager._get("taskId_X", 'deps:')), t1.file_dep)

    def test_save_values(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual({'x': 5, 'y': 10}, self.dep_manager._get("t1", "_values_:"))


class TestSaveSuccessJson(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'json'

class TestSaveSuccessSqlite(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestSaveSuccessDbmGnu(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestSaveSuccessDbmNdbm(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestSaveSuccessDbmDumb(DependencyTestBase, _SaveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestGetValue
# ---------------------------------------------------------------------------

class _GetValueTests:
    """Tests for Dependency.get_value / get_values."""

    def test_all_values(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual({'x': 5, 'y': 10}, self.dep_manager.get_values('t1'))

    def test_ok(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual(5, self.dep_manager.get_value('t1', 'x'))

    def test_ok_dot_on_task_name(self):
        t1 = Task('t1:a.ext', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertEqual(5, self.dep_manager.get_value('t1:a.ext', 'x'))

    def test_invalid_taskid(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertRaises(Exception, self.dep_manager.get_value, 'nonono', 'x')

    def test_invalid_key(self):
        t1 = Task('t1', None)
        t1.values = {'x': 5, 'y': 10}
        self.dep_manager.save_success(t1)
        self.assertRaises(Exception, self.dep_manager.get_value, 't1', 'z')


class TestGetValueJson(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'json'

class TestGetValueSqlite(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestGetValueDbmGnu(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestGetValueDbmNdbm(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestGetValueDbmDumb(DependencyTestBase, _GetValueTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestRemoveSuccess
# ---------------------------------------------------------------------------

class _RemoveSuccessTests:

    def test_save_result(self):
        t1 = Task('t_name', None)
        t1.result = "result"
        self.dep_manager.save_success(t1)
        self.assertEqual(get_md5("result"), self.dep_manager._get(t1.name, "result:"))
        self.dep_manager.remove_success(t1)
        self.assertIsNone(self.dep_manager._get(t1.name, "result:"))


class TestRemoveSuccessJson(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'json'

class TestRemoveSuccessSqlite(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestRemoveSuccessDbmGnu(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestRemoveSuccessDbmNdbm(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestRemoveSuccessDbmDumb(DependencyTestBase, _RemoveSuccessTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestIgnore
# ---------------------------------------------------------------------------

class _IgnoreTests:

    def test_save_result(self):
        t1 = Task('t_name', None)
        self.dep_manager.ignore(t1)
        self.assertEqual('1', self.dep_manager._get(t1.name, "ignore:"))


class TestIgnoreJson(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'json'

class TestIgnoreSqlite(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestIgnoreDbmGnu(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestIgnoreDbmNdbm(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestIgnoreDbmDumb(DependencyTestBase, _IgnoreTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# TestMD5Checker
# ---------------------------------------------------------------------------

class TestMD5Checker(DependencyFileMixin, unittest.TestCase):

    def test_timestamp(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0], state[1] + 1, '')
        file_stat = os.stat(self.dependency1)
        # dep considered the same as long as timestamp is unchanged
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))

    def test_size(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0] + 1, state[1] + 1, state[2])
        file_stat = os.stat(self.dependency1)
        # if size changed for sure modified (md5 is not checked)
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state2))

    def test_md5(self):
        checker = MD5Checker()
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        # same size and md5
        state2 = (state[0] + 1, state[1], state[2])
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))
        # same size, different md5
        state3 = (state[0] + 1, state[1], 'not me')
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state3))


# ---------------------------------------------------------------------------
# TestCustomChecker
# ---------------------------------------------------------------------------

class TestCustomChecker(DependencyFileMixin, unittest.TestCase):

    def test_not_implemented(self):
        class MyChecker(FileChangedChecker):
            pass

        checker = MyChecker()
        self.assertRaises(NotImplementedError, checker.get_state, None, None)
        self.assertRaises(NotImplementedError, checker.check_modified,
                          None, None, None)


# ---------------------------------------------------------------------------
# TestTimestampChecker
# ---------------------------------------------------------------------------

class TestTimestampChecker(DependencyFileMixin, unittest.TestCase):

    def test_timestamp(self):
        checker = TimestampChecker()
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state))
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state + 1))


# ---------------------------------------------------------------------------
# TestSHA256Checker
# ---------------------------------------------------------------------------

def rewrite_same_size(path, content):
    """Replace file content with same-length data, keeping the mtime.

    Simulates a copy that preserves timestamps (e.g. ``cp -p``), the
    scenario where timestamp-based checks miss a content change.
    """
    original_stat = os.stat(path)
    assert len(content) == original_stat.st_size
    with open(path, "w") as fd:
        fd.write(content)
    os.utime(path, (original_stat.st_atime, original_stat.st_mtime))


class TestSHA256Checker(DependencyFileMixin, unittest.TestCase):

    def test_state_format(self):
        checker = SHA256Checker()
        state = checker.get_state(self.dependency1, None)
        timestamp, size, digest = state
        self.assertEqual(os.path.getmtime(self.dependency1), timestamp)
        self.assertEqual(os.path.getsize(self.dependency1), size)
        self.assertEqual(get_file_sha256(self.dependency1), digest)
        self.assertEqual(64, len(digest))

    def test_timestamp_optimization(self):
        # non-strict mode: same timestamp means same file, content is
        # not hashed (same behavior as MD5Checker)
        checker = SHA256Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0], state[1] + 1, '')
        file_stat = os.stat(self.dependency1)
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))

    def test_size(self):
        checker = SHA256Checker()
        state = checker.get_state(self.dependency1, None)
        state2 = (state[0] + 1, state[1] + 1, state[2])
        file_stat = os.stat(self.dependency1)
        # if size changed for sure modified (sha256 is not checked)
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state2))

    def test_sha256(self):
        checker = SHA256Checker()
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        # same size and sha256
        state2 = (state[0] + 1, state[1], state[2])
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state2))
        # same size, different sha256
        state3 = (state[0] + 1, state[1], 'not me')
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state3))

    def test_same_timestamp_different_content(self):
        # strict mode detects content change even if mtime is preserved
        checker = SHA256Checker(strict=True)
        state = checker.get_state(self.dependency1, None)
        size = os.path.getsize(self.dependency1)
        rewrite_same_size(self.dependency1, 'X' * size)
        file_stat = os.stat(self.dependency1)
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state))
        # non-strict mode trusts the timestamp and misses the change
        fast_checker = SHA256Checker()
        self.assertFalse(
            fast_checker.check_modified(self.dependency1, file_stat, state))

    def test_strict_unmodified_file(self):
        # strict mode still considers an untouched file as not modified
        checker = SHA256Checker(strict=True)
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        self.assertFalse(checker.check_modified(self.dependency1, file_stat, state))

    def test_legacy_md5_state(self):
        # states saved by MD5Checker (32-chars digest) are never trusted:
        # always considered modified so the state is safely re-computed
        # and migrated to sha256 on the next successful save
        md5_checker = MD5Checker()
        legacy_state = md5_checker.get_state(self.dependency1, None)
        self.assertEqual(32, len(legacy_state[2]))
        checker = SHA256Checker()
        file_stat = os.stat(self.dependency1)
        # even with unchanged content and timestamp: needs re-computation
        self.assertTrue(
            checker.check_modified(self.dependency1, file_stat, legacy_state))
        # also on strict mode
        strict_checker = SHA256Checker(strict=True)
        self.assertTrue(
            strict_checker.check_modified(
                self.dependency1, file_stat, legacy_state))

    def test_get_state_skip_unchanged_timestamp(self):
        # get_state returns None if the saved timestamp is unchanged
        # (optimization, only on non-strict mode)
        checker = SHA256Checker()
        state = checker.get_state(self.dependency1, None)
        self.assertIsNone(checker.get_state(self.dependency1, state))
        # strict mode always computes a fresh state
        strict_checker = SHA256Checker(strict=True)
        self.assertIsNotNone(strict_checker.get_state(self.dependency1, state))

    def test_digest_cache(self):
        # repeated checks of the same file within a run share the digest
        state = SHA256Checker().get_state(self.dependency1, None)
        # force the hash path by using a different saved timestamp
        state = (state[0] + 1, state[1], state[2])
        checker = SHA256Checker()
        file_stat = os.stat(self.dependency1)
        with patch('doit.dependency.get_file_sha256',
                   wraps=get_file_sha256) as mock_sha:
            checker.check_modified(self.dependency1, file_stat, state)
            checker.check_modified(self.dependency1, file_stat, state)
        self.assertEqual(1, mock_sha.call_count)
        # but a modified file is never checked against a stale digest:
        # a normal modification updates the mtime, invalidating the cache
        size = os.path.getsize(self.dependency1)
        with open(self.dependency1, "w") as fd:
            fd.write('Z' * size)
        os.utime(self.dependency1, (file_stat.st_atime, file_stat.st_mtime + 2))
        file_stat = os.stat(self.dependency1)
        self.assertTrue(checker.check_modified(self.dependency1, file_stat, state))

    def test_strict_no_digest_cache(self):
        # strict mode never re-uses a cached digest: the content is read
        # from disk on every check
        checker = SHA256Checker(strict=True)
        state = checker.get_state(self.dependency1, None)
        file_stat = os.stat(self.dependency1)
        with patch('doit.dependency.get_file_sha256',
                   wraps=get_file_sha256) as mock_sha:
            checker.check_modified(self.dependency1, file_stat, state)
            checker.check_modified(self.dependency1, file_stat, state)
        self.assertEqual(2, mock_sha.call_count)


# ---------------------------------------------------------------------------
# TestDependencyStatus
# ---------------------------------------------------------------------------

class TestDependencyStatus(unittest.TestCase):

    def test_add_reason(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.add_reason('changed_file_dep', 'f1'))
        self.assertEqual('run', result.status)
        self.assertFalse(result.add_reason('changed_file_dep', 'f2'))
        self.assertEqual(['f1', 'f2'], result.reasons['changed_file_dep'])

    def test_add_reason_error(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.add_reason('missing_file_dep', 'f1', 'error'))
        self.assertEqual('error', result.status)
        self.assertEqual(['f1'], result.reasons['missing_file_dep'])

    def test_set_reason(self):
        result = DependencyStatus(True)
        self.assertEqual('up-to-date', result.status)
        self.assertFalse(result.set_reason('has_no_dependencies', True))
        self.assertEqual('run', result.status)
        self.assertTrue(result.reasons['has_no_dependencies'])

    def test_no_log(self):
        result = DependencyStatus(False)
        self.assertEqual('up-to-date', result.status)
        self.assertTrue(result.set_reason('has_no_dependencies', True))
        self.assertEqual('run', result.status)

    def test_get_error_message(self):
        result = DependencyStatus(False)
        self.assertIsNone(result.get_error_message())
        result.error_reason = 'foo xxx'
        self.assertEqual('foo xxx', result.get_error_message())


# ---------------------------------------------------------------------------
# TestGetStatus
# ---------------------------------------------------------------------------

class _GetStatusTests:
    """Tests for Dependency.get_status."""

    def test_ignore(self):
        t1 = Task("t1", None)
        # before ignore
        self.assertFalse(self.dep_manager.status_is_ignore(t1))
        # after ignote
        self.dep_manager.ignore(t1)
        self.assertTrue(self.dep_manager.status_is_ignore(t1))

    def test_fileDependencies(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        dependencies = [filePath]
        t1 = Task("t1", None, dependencies)

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(dependencies, t1.dep_changed)

        # second time no
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # FIXME - mock timestamp
        time.sleep(1)  # required otherwise timestamp is not modified!
        # a small change on the file
        ff = open(filePath, "a")
        ff.write(" part2")
        ff.close()

        # execute again
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(dependencies, t1.dep_changed)

    def test_fileDependencies_changed(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part1")
        ff.close()

        dependencies = [filePath, filePath2]
        t1 = Task("t1", None, dependencies)

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(sorted(dependencies), sorted(t1.dep_changed))

        # second time no
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # remove dependency filePath2
        t1 = Task("t1", None, [filePath])
        # execute again
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_fileDependencies_changed_get_log(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        filePath2 = get_abspath("data/dependency2")
        ff = open(filePath2, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, [filePath])

        # first time execute
        result = self.dep_manager.get_status(t1, {}, get_log=True)
        self.assertEqual('run', result.status)
        self.assertEqual([filePath], t1.dep_changed)
        self.dep_manager.save_success(t1)

        # second time
        t1b = Task("t1", None, [filePath2])
        result = self.dep_manager.get_status(t1b, {}, get_log=True)
        self.assertEqual('run', result.status)
        self.assertEqual([filePath2], t1b.dep_changed)
        self.assertEqual([filePath], result.reasons['removed_file_dep'])
        self.assertEqual([filePath2], result.reasons['added_file_dep'])

    def test_file_dependency_not_exist(self):
        filePath = get_abspath("data/dependency_not_exist")
        t1 = Task("t1", None, [filePath])
        self.assertEqual('error', self.dep_manager.get_status(t1, {}).status)

    def test_change_checker(self):
        # need a real dependency file
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("checker test content")
        ff.close()

        t1 = Task("taskId_X", None, [dep_path])
        self.dep_manager.checker = TimestampChecker()
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        # change of checker force `run` again
        self.dep_manager.checker = MD5Checker()
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    # if there is no dependency the task is always executed
    def test_noDependency(self):
        t1 = Task("t1", None)
        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)
        # second too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateFalse(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[False])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateTrue(self):
        t1 = Task("t1", None, uptodate=[True])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateNone(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[None])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([filePath], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateFunction_True(self):
        test_case = self

        def check(task, values):
            test_case.assertEqual('t1', task.name)
            return True

        t1 = Task("t1", None, uptodate=[check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateFunction_False(self):
        filePath = get_abspath("data/dependency1")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        def check(task, values):
            return False

        t1 = Task("t1", None, file_dep=[filePath], uptodate=[check])

        # first time execute
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

        # second time execute too
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_UptodateFunction_without_args_True(self):
        def check():
            return True

        t1 = Task("t1", None, uptodate=[check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_uptodate_call_all_even_if_some_False(self):
        checks = []

        def check():
            checks.append(1)
            return False

        t1 = Task("t1", None, uptodate=[check, check])
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(2, len(checks))

    def test_UptodateFunction_extra_args_True(self):
        test_case = self

        def check(task, values, control):
            test_case.assertEqual('t1', task.name)
            return control > 30

        t1 = Task("t1", None, uptodate=[(check, [34])])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCallable_True(self):
        test_case = self

        class MyChecker(object):
            def __call__(self, task, values):
                test_case.assertEqual('t1', task.name)
                return True

        t1 = Task("t1", None, uptodate=[MyChecker()])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateMethod_True(self):
        test_case = self

        class MyChecker(object):
            def check(self, task, values):
                test_case.assertEqual('t1', task.name)
                return True

        t1 = Task("t1", None, uptodate=[MyChecker().check])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCallable_added_attributes(self):
        test_case = self
        task_dict = "fake dict"

        class My_uptodate(UptodateCalculator):
            def __call__(self, task, values):
                # attributes were added to object before call'ing it
                test_case.assertEqual(task_dict, self.tasks_dict)
                test_case.assertIsNone(self.get_val('t1', None))
                return True

        check = My_uptodate()
        t1 = Task("t1", None, uptodate=[check])
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, task_dict).status)

    def test_UptodateCommand_True(self):
        t1 = Task("t1", None, uptodate=[PROGRAM])
        self.dep_manager.save_success(t1)
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)

    def test_UptodateCommand_False(self):
        t1 = Task("t1", None, uptodate=[PROGRAM + ' please fail'])
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)

    # if target file does not exist, task is outdated.
    def test_targets_notThere(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        target = get_abspath("data/target")
        if os.path.exists(target):
            os.remove(target)

        t1 = Task("task x", None, [dep_path], [target])
        self.dep_manager.save_success(t1)
        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([dep_path], t1.dep_changed)

    def test_targets(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        filePath = get_abspath("data/target")
        ff = open(filePath, "w")
        ff.write("part1")
        ff.close()

        deps = [dep_path]
        targets = [filePath]
        t1 = Task("task X", None, deps, targets)

        self.dep_manager.save_success(t1)
        # up-to-date because target exist
        self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual([], t1.dep_changed)

    def test_targetFolder(self):
        dep_path = get_abspath("data/dependency1")
        ff = open(dep_path, "w")
        ff.write("dep content")
        ff.close()

        # folder not there. task is not up-to-date
        deps = [dep_path]
        folderPath = get_abspath("data/target-folder")
        if os.path.exists(folderPath):
            os.rmdir(folderPath)
        t1 = Task("task x", None, deps, [folderPath])
        self.dep_manager.save_success(t1)

        self.assertEqual('run', self.dep_manager.get_status(t1, {}).status)
        self.assertEqual(deps, t1.dep_changed)
        # create folder. task is up-to-date
        os.mkdir(folderPath)
        try:
            self.assertEqual('up-to-date', self.dep_manager.get_status(t1, {}).status)
            self.assertEqual([], t1.dep_changed)
        finally:
            if os.path.exists(folderPath):
                os.rmdir(folderPath)


class TestGetStatusJson(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'json'

class TestGetStatusSqlite(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'sqlite3'

class TestGetStatusDbmGnu(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.gnu'

class TestGetStatusDbmNdbm(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.ndbm'

class TestGetStatusDbmDumb(DependencyTestBase, _GetStatusTests, unittest.TestCase):
    backend_name = 'dbm.dumb'


# ---------------------------------------------------------------------------
# SHA256Checker integration tests (per backend)
# ---------------------------------------------------------------------------

class _SHA256DepTests:
    """Integration tests for SHA256Checker against real DB backends.

    Uses small files with equal length, same mtime but different content,
    reproducing the "copy preserving mtime" scenario without touching
    file timestamps to work around the check.
    """
    backend_cls = None

    def _reopen(self, checker_cls=MD5Checker, checker_kwargs=None):
        """close current dep_manager and re-open the same DB file"""
        if not self.dep_manager._closed:
            self.dep_manager.close()
        cls = checker_cls
        if checker_kwargs is not None:
            cls = (checker_cls, checker_kwargs)
        self.dep_manager = Dependency(
            self.backend_cls, self.dep_manager.name, checker_cls=cls)
        return self.dep_manager

    def _write_small_file(self, content):
        path = os.path.join(self._dep_tmpdir, 'small-dep.txt')
        with open(path, 'w') as fd:
            fd.write(content)
        return path

    def test_copy_preserving_mtime(self):
        # small file, same length, same mtime, different content
        dep_path = self._write_small_file('abc')
        t1 = Task('t1', None, [dep_path])
        dep = self._reopen(SHA256Checker, {'strict': True})
        self.assertEqual('run', dep.get_status(t1, {}).status)
        dep.save_success(t1)
        self.assertEqual('up-to-date', dep.get_status(t1, {}).status)

        # replace content with same-length data, preserving the mtime
        # (like `cp -p` of a modified copy over the original file)
        rewrite_same_size(dep_path, 'xyz')

        # strict mode re-checks the content and detects the change
        self.assertEqual('run', dep.get_status(t1, {}).status)
        self.assertEqual([dep_path], t1.dep_changed)

        # non-strict mode keeps the timestamp optimization and would
        # miss this change (documented behavior)
        dep_fast = self._reopen(SHA256Checker)
        self.assertEqual('up-to-date', dep_fast.get_status(t1, {}).status)

    def test_migrate_from_md5(self):
        # save state with the old MD5Checker
        dep_path = self._write_small_file('abc')
        t1 = Task('t1', None, [dep_path])
        self.dep_manager.save_success(t1)
        old_state = self.dep_manager._get('t1', dep_path)
        self.assertEqual(32, len(old_state[2]))  # md5 hexdigest

        # re-open old DB with sha256 checker: task must run once to
        # migrate the state (legacy md5 state is never trusted)
        dep = self._reopen(SHA256Checker)
        self.assertEqual('run', dep.get_status(t1, {}).status)
        self.assertEqual([dep_path], t1.dep_changed)

        # after a successful run the state is migrated to sha256
        dep.save_success(t1)
        new_state = dep._get('t1', dep_path)
        self.assertEqual(64, len(new_state[2]))  # sha256 hexdigest
        self.assertEqual('SHA256Checker', dep._get('t1', 'checker:'))

        # and now the task is up-to-date again
        self.assertEqual('up-to-date', dep.get_status(t1, {}).status)

        # strict mode also migrates old md5 states
        dep_strict = self._reopen(SHA256Checker, {'strict': True})
        self.assertEqual('up-to-date', dep_strict.get_status(t1, {}).status)

    def test_switch_back_to_md5(self):
        # save state with sha256 checker
        dep_path = self._write_small_file('abc')
        t1 = Task('t1', None, [dep_path])
        dep = self._reopen(SHA256Checker)
        dep.save_success(t1)
        self.assertEqual('up-to-date', dep.get_status(t1, {}).status)

        # switching back to md5 forces a re-run (checker changed)
        dep_md5 = self._reopen(MD5Checker)
        self.assertEqual('run', dep_md5.get_status(t1, {}).status)
        dep_md5.save_success(t1)
        self.assertEqual('up-to-date', dep_md5.get_status(t1, {}).status)
        self.assertEqual('MD5Checker', dep_md5._get('t1', 'checker:'))

    def test_failed_task_saves_no_state(self):
        # new success evidence is only saved after the task actually
        # succeeds (save_success is not called for failed tasks)
        dep_path = self._write_small_file('abc')
        t1 = Task('t1', None, [dep_path])
        dep = self._reopen(SHA256Checker, {'strict': True})
        self.assertEqual('run', dep.get_status(t1, {}).status)
        # task fails: no save_success call, no state was persisted
        self.assertIsNone(dep._get('t1', dep_path))
        self.assertIsNone(dep._get('t1', 'checker:'))
        # next run still considers the task out-of-date
        self.assertEqual('run', dep.get_status(t1, {}).status)
        # after a real success the evidence is saved
        dep.save_success(t1)
        self.assertEqual('up-to-date', dep.get_status(t1, {}).status)

    def test_digest_shared_between_tasks(self):
        # two tasks depending on the same file share the digest computed
        # within the same run (non-strict mode keeps the optimization)
        dep_path = self._write_small_file('abc')
        t1 = Task('t1', None, [dep_path])
        t2 = Task('t2', None, [dep_path])
        dep = self._reopen(SHA256Checker)
        dep.save_success(t1)
        dep.save_success(t2)
        # force hashing by bumping the saved timestamp, then re-open so
        # the checker starts with an empty digest cache
        for task in (t1, t2):
            state = dep._get(task.name, dep_path)
            dep._set(task.name, dep_path, (state[0] + 1, state[1], state[2]))
        dep = self._reopen(SHA256Checker)
        with patch('doit.dependency.get_file_sha256',
                   wraps=get_file_sha256) as mock_sha:
            self.assertEqual('up-to-date', dep.get_status(t1, {}).status)
            self.assertEqual('up-to-date', dep.get_status(t2, {}).status)
        self.assertEqual(1, mock_sha.call_count)

        # strict mode never trusts a cached digest: a later change must
        # always be re-read from disk even with a preserved mtime
        dep_strict = self._reopen(SHA256Checker, {'strict': True})
        with patch('doit.dependency.get_file_sha256',
                   wraps=get_file_sha256) as mock_sha_strict:
            dep_strict.get_status(t1, {})
            rewrite_same_size(dep_path, 'xyz')
            dep_strict.get_status(t2, {})
        self.assertEqual(2, mock_sha_strict.call_count)


class TestSHA256DepJson(DependencyTestBase, _SHA256DepTests, unittest.TestCase):
    backend_name = 'json'
    backend_cls = JsonDB


class TestSHA256DepSqlite(DependencyTestBase, _SHA256DepTests, unittest.TestCase):
    backend_name = 'sqlite3'
    backend_cls = SqliteDB
