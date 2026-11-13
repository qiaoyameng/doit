"""Tests for per-task environment declarations."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from doit.dependency import Dependency, DbmDB, JsonDB
from doit.exceptions import TaskError
from doit.reporter import ConsoleReporter
from doit.runner import MRunner, MThreadRunner
from doit.task import Stream, Task
from doit.control import TaskDispatcher


def _write_env(path, names):
    """record selected variables for the action's process."""
    data = {name: os.environ.get(name) for name in names}
    data['pid'] = os.getpid()
    data['ppid'] = os.getppid()
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(data, stream)
    return True


def _count(path):
    """increment an execution counter."""
    try:
        with open(path, encoding='utf-8') as stream:
            count = int(stream.read())
    except FileNotFoundError:
        count = 0
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(str(count + 1))
    return True


def _mutate_env(path, names):
    """mutate os.environ and record what was seen before mutating."""
    data = {name: os.environ.get(name) for name in names}
    os.environ['TZ'] = 'MUTATED'
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(data, stream)
    return True


class EnvTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix='doit-env-')
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._old_env = os.environ.copy()
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)


class TestTaskEnv(EnvTestCase):
    def test_inherit_override_and_delete(self):
        os.environ['INHERITED'] = 'base'
        os.environ['DELETED'] = 'bye'
        task = Task('t', [], env={'TZ': 'UTC', 'DELETED': None})
        snapshot = task.get_env_snapshot()
        self.assertEqual('base', snapshot['INHERITED'])
        self.assertEqual('UTC', snapshot['TZ'])
        self.assertNotIn('DELETED', snapshot)

    def test_snapshot_is_fixed_per_task_start(self):
        os.environ['MODE'] = 'one'
        task = Task('t', [], env_uptodate=['MODE'])
        task.begin_env()
        first = task.get_env_snapshot()
        os.environ['MODE'] = 'two'
        self.assertEqual('one', first['MODE'])
        self.assertEqual('one', task.get_env_snapshot()['MODE'])

    def test_no_declaration_keeps_old_behavior(self):
        task = Task('t', [])
        self.assertFalse(task.has_env_config)
        self.assertIsNone(task.get_env_snapshot())
        self.assertIsNone(task.env_metadata())

    def test_cmd_action_gets_independent_env(self):
        os.environ['PARENT_ONLY'] = 'parent'
        os.environ['DELETED'] = 'bye'
        command = [
            os.path.abspath(sys.executable), '-c',
            ('import json, os; print(json.dumps({k: os.environ.get(k) for k '
             'in ("TZ", "PARENT_ONLY", "DELETED", "ADDED")}))')]
        task = Task('t', [command], env={
            'TZ': 'UTC', 'DELETED': None, 'ADDED': 'yes'})
        task.init_options()
        result = task.actions[0].execute()
        self.assertIsNone(result)
        got = json.loads(task.actions[0].out)
        self.assertEqual({
            'TZ': 'UTC', 'PARENT_ONLY': 'parent',
            'DELETED': None, 'ADDED': 'yes'}, got)
        self.assertEqual('parent', os.environ['PARENT_ONLY'])
        self.assertEqual('bye', os.environ['DELETED'])
        self.assertNotIn('TZ', os.environ)
        self.assertNotIn('ADDED', os.environ)

    def test_python_action_refuses_without_isolation(self):
        os.environ['BASE'] = 'unchanged'
        task = Task('t', [(lambda: True)], env={'BASE': 'changed'})
        task.init_options()
        result = task.actions[0].execute()
        self.assertIsInstance(result, TaskError)
        self.assertIn('isolated process', str(result))
        self.assertEqual('unchanged', os.environ['BASE'])

    def test_python_action_refuses_in_thread_runner(self):
        os.environ['BASE'] = 'unchanged'
        task = Task('t', [(lambda: True)], env={'BASE': 'changed'})
        task.init_options()
        result = task.execute(
            Stream(0), env_isolation=MThreadRunner.env_isolation)
        self.assertIsInstance(result, TaskError)
        self.assertEqual('unchanged', os.environ['BASE'])

    @unittest.skipIf(not MRunner.available(), 'MRunner not available')
    def test_parallel_python_actions_are_isolated(self):
        os.environ['PARENT_ONLY'] = 'parent'
        one = self.tmp / 'one.json'
        two = self.tmp / 'two.json'
        tasks = {
            'one': Task('one', [(_write_env, (str(one),),
                                 {'names': ['TZ', 'PARENT_ONLY']})],
                        env={'TZ': 'UTC'}),
            'two': Task('two', [(_write_env, (str(two),),
                                 {'names': ['TZ', 'PARENT_ONLY']})],
                        env={'TZ': 'Asia/Shanghai'}),
        }
        dep_manager = Dependency(DbmDB, str(self.tmp / 'db'))
        self.addCleanup(dep_manager.close)
        reporter = ConsoleReporter(io.StringIO(), {})
        runner = MRunner(dep_manager, reporter, num_process=2,
                         stream=Stream(0))
        runner.run_all(TaskDispatcher(tasks, [], ['one', 'two']))

        got_one = json.loads(one.read_text(encoding='utf-8'))
        got_two = json.loads(two.read_text(encoding='utf-8'))
        self.assertEqual('UTC', got_one['TZ'])
        self.assertEqual('Asia/Shanghai', got_two['TZ'])
        self.assertEqual('parent', got_one['PARENT_ONLY'])
        self.assertEqual('parent', got_two['PARENT_ONLY'])
        self.assertEqual(os.getpid(), got_one['ppid'])
        self.assertEqual(os.getpid(), got_two['ppid'])
        self.assertEqual('parent', os.environ['PARENT_ONLY'])
        self.assertNotIn('TZ', os.environ)

    @unittest.skipIf(not MRunner.available(), 'MRunner not available')
    def test_actions_share_task_snapshot(self):
        path = self.tmp / 'seen.json'
        task = Task('t', [
            (_mutate_env, (str(path),), {'names': ['TZ']}),
            (_write_env, (str(path),), {'names': ['TZ']}),
        ], env={'TZ': 'fixed'})
        dep_manager = Dependency(DbmDB, str(self.tmp / 'db'))
        self.addCleanup(dep_manager.close)
        reporter = ConsoleReporter(io.StringIO(), {})
        runner = MRunner(dep_manager, reporter, num_process=1,
                         stream=Stream(0))
        runner.run_all(TaskDispatcher({'t': task}, [], ['t']))
        # the second action sees the fixed snapshot, not the first action's
        # process-local mutation.
        got = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual('fixed', got['TZ'])


class TestEnvUptodate(EnvTestCase):
    def test_changed_selected_env_reruns_only_that_task(self):
        dep_manager = Dependency(DbmDB, str(self.tmp / 'db'))
        self.addCleanup(dep_manager.close)

        def tasks(tz_one, tz_two):
            return {
                'one': Task('one', [_count],
                            targets=[str(self.tmp / 'one')],
                            env={'TZ': tz_one}),
                'two': Task('two', [_count],
                            targets=[str(self.tmp / 'two')],
                            env={'TZ': tz_two}),
            }

        # up-to-date is based on the declaration; fake stored success
        first = tasks('UTC', 'Asia/Shanghai')
        for task in first.values():
            Path(task.targets[0]).write_text('x')
            dep_manager.save_success(task)

        second = tasks('UTC', 'Asia/Shanghai')
        for task in second.values():
            self.assertEqual(
                'up-to-date',
                dep_manager.get_status(task, second).status)

        changed = tasks('CET', 'Asia/Shanghai')
        self.assertEqual(
            'run', dep_manager.get_status(changed['one'], changed).status)
        self.assertEqual(
            'up-to-date',
            dep_manager.get_status(changed['two'], changed).status)

    def test_sensitive_values_are_not_persisted(self):
        db_path = str(self.tmp / 'db.json')
        dep_manager = Dependency(JsonDB, db_path)
        self.addCleanup(dep_manager.close)
        secret = 'super-secret-value'
        task = Task('t', [],
                    targets=[str(self.tmp / 'target')],
                    env={'TZ': 'UTC', 'DB_PASSWORD': secret})
        Path(task.targets[0]).write_text('x')
        dep_manager.save_success(task)
        dep_manager.close()

        raw_db = Path(db_path).read_text(encoding='utf-8')
        self.assertNotIn(secret, raw_db)
        metadata = json.loads(raw_db)['t']['env:']
        self.assertEqual('DB_PASSWORD', metadata['DB_PASSWORD']['name'])
        self.assertTrue(metadata['DB_PASSWORD']['sensitive'])
        self.assertNotIn('value', metadata['DB_PASSWORD'])
        self.assertTrue(metadata['DB_PASSWORD']['fingerprint'].startswith(
            'sha256:'))

        reopened = Dependency(JsonDB, db_path)
        self.addCleanup(reopened.close)
        status = reopened.get_status(
            Task('t', [],
                 env={'TZ': 'UTC', 'DB_PASSWORD': 'other-secret'}), {},
            get_log=True)
        self.assertEqual('run', status.status)
        self.assertEqual([['DB_PASSWORD']], status.reasons['env_changed'])

    def test_explicit_uptodate_selection(self):
        dep_manager = Dependency(DbmDB, str(self.tmp / 'db'))
        self.addCleanup(dep_manager.close)
        task = Task('t', [], env={'USED': '1', 'IGNORED': 'a'},
                    env_uptodate=['USED'])
        dep_manager.save_success(task)
        changed = Task('t', [], env={'USED': '1', 'IGNORED': 'b'},
                       env_uptodate=['USED'])
        self.assertEqual(
            'up-to-date', dep_manager.get_status(changed, {}).status)


if __name__ == '__main__':
    unittest.main()
