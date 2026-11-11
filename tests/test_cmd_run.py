import os
import io
import shutil
import tempfile
import contextlib
import unittest
from io import StringIO
from unittest.mock import Mock, patch

from doit.exceptions import InvalidCommand
from doit import reporter, runner
from doit.cmd_run import Run
from doit.task import Task, DelayedLoader
from tests.support import tasks_sample, CmdFactory
from tests.support import DepfileNameMixin, DependencyFileMixin
from tests.support import RestoreCwdMixin


class TestCmdRun(DependencyFileMixin, DepfileNameMixin, unittest.TestCase):

    def testProcessRun(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(self.dependency1))
        result = cmd_run._execute(output)
        self.assertEqual(0, result)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([".  t1", ".  t2", ".  g1.a", ".  g1.b", ".  t3"], got)

    @unittest.skipIf(not runner.MRunner.available(), 'MRunner not available')
    def testProcessRunMP(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(self.dependency1))
        result = cmd_run._execute(output, num_process=1)
        self.assertEqual(0, result)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([".  t1", ".  t2", ".  g1.a", ".  g1.b", ".  t3"], got)

    def testProcessRunMThread(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(self.dependency1))
        result = cmd_run._execute(output, num_process=1, par_type='thread')
        self.assertEqual(0, result)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([".  t1", ".  t2", ".  g1.a", ".  g1.b", ".  t3"], got)

    def testInvalidParType(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample())
        self.assertRaises(InvalidCommand, cmd_run._execute,
                          output, num_process=1, par_type='not_exist')

    def testMP_not_available(self):
        err = io.StringIO()
        with patch.object(runner.MRunner, "available",
                          Mock(return_value=False)):
            with contextlib.redirect_stderr(err):
                output = StringIO()
                cmd_run = CmdFactory(
                    Run, backend='dbm', dep_file=self.depfile_name,
                    task_list=tasks_sample(self.dependency1))
                result = cmd_run._execute(output, num_process=1)
                self.assertEqual(0, result)
                got = output.getvalue().split("\n")[:-1]
                self.assertEqual(
                    [".  t1", ".  t2", ".  g1.a", ".  g1.b", ".  t3"], got)
        err_output = err.getvalue()
        self.assertIn("WARNING:", err_output)
        self.assertIn("parallel using threads", err_output)

    def testProcessRunFilter(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(), sel_tasks=["g1.a"])
        cmd_run._execute(output)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([".  g1.a"], got)

    def testProcessRunSingle(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(), sel_tasks=["t3"])
        cmd_run._execute(output, single=True)
        got = output.getvalue().split("\n")[:-1]
        # t1 is a dependency of t3 but not included
        self.assertEqual([".  t3"], got)

    def testProcessRunSingleSubtasks(self):
        output = StringIO()
        task_list = tasks_sample()
        self.assertEqual(task_list[4].name, 'g1.b')
        task_list[4].task_dep = ['t3']
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=task_list, sel_tasks=["g1"])
        cmd_run._execute(output, single=True)
        got = output.getvalue().split("\n")[:-1]
        # t3 is a dependency of g1.b but not included
        self.assertEqual([".  g1.a", ".  g1.b"], got)

    def testProcessRunSingleWithArgs(self):
        output = StringIO()
        task_list = tasks_sample()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=task_list,
                             sel_tasks=["t1", "--arg1", "ABC"])
        cmd_run._execute(output, single=True)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([".  t1"], got)

    def testProcessRunEmptyFilter(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(), sel_tasks=[])
        cmd_run._execute(output)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual([], got)


class MyReporter(reporter.ConsoleReporter):
    def get_status(self, task):
        self.outstream.write('MyReporter.start %s\n' % task.name)


class TestCmdRunReporter(DepfileNameMixin, unittest.TestCase):

    def testReporterInstance(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=[tasks_sample()[0]])
        cmd_run._execute(output, reporter=MyReporter(output, {}))
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual('MyReporter.start t1', got[0])

    def testCustomReporter(self):
        output = StringIO()
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=[tasks_sample()[0]])
        cmd_run._execute(output, reporter=MyReporter)
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual('MyReporter.start t1', got[0])

    def testPluginReporter(self):
        output = StringIO()
        cmd_run = CmdFactory(
            Run, backend='dbm',
            dep_file=self.depfile_name,
            task_list=[tasks_sample()[0]],
            config={'REPORTER': {'my': 'tests.test_cmd_run:MyReporter'}})
        cmd_run._execute(output, reporter='my')
        got = output.getvalue().split("\n")[:-1]
        self.assertEqual('MyReporter.start t1', got[0])


class TestCmdRunOptions(DepfileNameMixin, unittest.TestCase):

    def test_outfile(self):
        cmd_run = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                             task_list=tasks_sample(), sel_tasks=["g1.a"])
        cmd_run._execute('test.out')
        try:
            outfile = open('test.out', 'r')
            got = outfile.read()
            outfile.close()
            self.assertEqual(".  g1.a\n", got)
        finally:
            if os.path.exists('test.out'):
                os.remove('test.out')


class TestCmdRunDelayedBatch(RestoreCwdMixin, DepfileNameMixin,
                             unittest.TestCase):
    """Delayed loader batches are published atomically.

    A small generator produces ten tasks in three flavors: raising on the
    last task, closing a dependency cycle, and completing normally.
    Executed actions are recorded in `self.executed`.
    """

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix='doit-delayed-')
        os.chdir(self.tmpdir)
        with open('input.txt', 'w') as fp:
            fp.write('input')
        self.executed = []
        self.creator_calls = 0

    def tearDown(self):
        super().tearDown()  # RestoreCwdMixin restores cwd
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rec(self, name, target=None):
        def action():
            if target:
                with open(target, 'w') as fp:
                    fp.write(name)
            self.executed.append(name)
        return action

    def _seed_action(self):
        def action():
            with open('seed.out', 'w') as fp:
                fp.write('seed')
            self.executed.append('seed')
        return action

    def _make_tasks(self, creator):
        """fresh task objects for every run (as a new process would)"""
        seed = Task('seed', [self._seed_action()],
                    file_dep=['input.txt'], targets=['seed.out'])
        late = Task('late', None,
                    loader=DelayedLoader(creator, executed='seed'))
        return [seed, late]

    def _run(self, creator):
        cmd = CmdFactory(Run, backend='dbm', dep_file=self.depfile_name,
                         task_list=self._make_tasks(creator))
        output = StringIO()
        result = cmd._execute(output)
        return result, output.getvalue()

    def _creator_raise_at_end(self):
        def creator():
            self.creator_calls += 1
            for i in range(9):
                target = f'gen{i}.out'
                yield {'name': f'gen{i}',
                       'actions': [self._rec(f'gen{i}', target)],
                       'targets': [target]}
            raise RuntimeError('boom on 10th')
        return creator

    def _creator_cycle_at_end(self):
        def creator():
            self.creator_calls += 1
            for i in range(9):
                target = f'gen{i}.out'
                yield {'name': f'gen{i}',
                       'actions': [self._rec(f'gen{i}', target)],
                       'targets': [target],
                       'task_dep': ['late:gen9'] if i == 0 else []}
            yield {'name': 'gen9',
                   'actions': [self._rec('gen9', 'gen9.out')],
                   'targets': ['gen9.out'], 'task_dep': ['late:gen0']}
        return creator

    def _creator_ok(self, duplicate_target=False):
        def creator():
            self.creator_calls += 1
            for i in range(9):
                target = f'gen{i}.out'
                yield {'name': f'gen{i}',
                       'actions': [self._rec(f'gen{i}', target)],
                       'targets': [target]}
            # 10th task: optionally reuses gen0's target
            target = 'gen0.out' if duplicate_target else 'gen9.out'
            yield {'name': 'gen9',
                   'actions': [self._rec('gen9', target)],
                   'targets': [target]}
        return creator

    def test_batch_raises_at_end(self):
        """generator raises on 10th task: zero batch actions executed"""
        result, output = self._run(self._creator_raise_at_end())
        self.assertEqual(runner.ERROR, result)
        # only the task executed before the loader ran
        self.assertEqual(['seed'], self.executed)
        # error message points to the generation source
        self.assertIn("Delayed task creation 'late'", output)
        self.assertIn('boom on 10th', output)
        # nothing from the batch was published
        self.assertEqual([], [f for f in os.listdir('.')
                              if f.startswith('gen')])
        # generator was consumed exactly once
        self.assertEqual(1, self.creator_calls)

    def test_batch_cycle_at_end(self):
        """10th task closes a cycle: batch rejected, no deadlock"""
        result, output = self._run(self._creator_cycle_at_end())
        self.assertEqual(runner.ERROR, result)
        self.assertEqual(['seed'], self.executed)
        self.assertIn('cyclic dependencies', output)
        self.assertIn('late:gen0', output)
        self.assertIn('late:gen9', output)
        self.assertEqual([], [f for f in os.listdir('.')
                              if f.startswith('gen')])

    def test_batch_duplicate_target_then_fixed(self):
        """failed batch leaves no residue; fixed run is clean"""
        result, output = self._run(self._creator_ok(duplicate_target=True))
        self.assertEqual(runner.ERROR, result)
        self.assertEqual(['seed'], self.executed)
        self.assertIn('late:gen0', output)
        self.assertIn('late:gen9', output)
        self.assertEqual([], [f for f in os.listdir('.')
                              if f.startswith('gen')])

        # fix the duplicate target and run again
        result, output = self._run(self._creator_ok())
        self.assertEqual(runner.SUCCESS, result)
        # seed was already successful and is not executed again
        self.assertEqual(['seed'] + [f'gen{i}' for i in range(10)],
                         self.executed)
        # no stale duplicated nodes from the failed run: every generated
        # task is dispatched exactly once
        for i in range(10):
            self.assertEqual(1, output.count(f'.  late:gen{i}\n'))
        self.assertEqual(10, len([f for f in os.listdir('.')
                                  if f.startswith('gen')]))

    def test_batch_success(self):
        """normal completion: whole batch published and executed once"""
        result, output = self._run(self._creator_ok())
        self.assertEqual(runner.SUCCESS, result)
        self.assertEqual(['seed'] + [f'gen{i}' for i in range(10)],
                         self.executed)
        self.assertEqual(1, self.creator_calls)
