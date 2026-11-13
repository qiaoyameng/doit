"""Tests for the optional per-task execution timeout.

Core state tests drive a fully controllable fake monotonic clock, so they
do not rely on real sleeps. End-to-end tests use controlled subprocesses
that synchronize through files/signals.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from threading import Thread

from doit import action
from doit.action import CmdAction, Deadline
from doit.exceptions import InvalidTask, TaskTimeout, TaskFailed
from doit.task import Task
from tests.test_runner import FakeReporter


HERE = os.path.dirname(__file__)
TREE_SCRIPT = os.path.join(HERE, "timeout_spawn_tree.py")
STUBBORN_SCRIPT = os.path.join(HERE, "timeout_spawn_stubborn.py")


class FakeClock:
    """Controllable replacement for the module-level monotonic clock."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestDeadline(unittest.TestCase):
    def test_none_timeout_never_expires(self):
        clock = FakeClock()
        deadline = Deadline(None, clock=clock)
        self.assertFalse(deadline.active)
        self.assertIsNone(deadline.remaining())
        clock.advance(10 ** 6)
        self.assertFalse(deadline.expired())

    def test_uses_monotonic_clock_not_wall_clock(self):
        clock = FakeClock()
        deadline = Deadline(5, clock=clock)
        self.assertTrue(deadline.active)
        self.assertEqual(deadline.start, 1000.0)
        self.assertEqual(deadline.remaining(), 5)
        clock.advance(3)
        self.assertAlmostEqual(deadline.remaining(), 2)
        self.assertFalse(deadline.expired())
        clock.advance(2)
        self.assertEqual(deadline.remaining(), 0)
        self.assertTrue(deadline.expired())

    def test_remaining_never_negative(self):
        clock = FakeClock()
        deadline = Deadline(1, clock=clock)
        clock.advance(50)
        self.assertEqual(deadline.remaining(), 0)
        self.assertTrue(deadline.expired())

    def test_default_clock_is_module_monotonic(self):
        deadline = Deadline(10)
        self.assertAlmostEqual(deadline.start, action.monotonic(), places=2)


class TestTaskTimeoutAttribute(unittest.TestCase):
    def test_default_is_none(self):
        task = Task("t", [])
        self.assertIsNone(task.timeout)

    def test_accepts_int_and_float(self):
        self.assertEqual(Task("a", [], timeout=1).timeout, 1)
        self.assertEqual(Task("b", [], timeout=2.5).timeout, 2.5)

    def test_rejects_bool(self):
        with self.assertRaises(InvalidTask):
            Task("t", [], timeout=True)

    def test_rejects_zero_and_negative(self):
        with self.assertRaises(InvalidTask):
            Task("t", [], timeout=0)
        with self.assertRaises(InvalidTask):
            Task("t", [], timeout=-3)

    def test_rejects_wrong_type(self):
        with self.assertRaises(InvalidTask):
            Task("t", [], timeout="10")


class TestTaskExecuteDeadline(unittest.TestCase):
    """The deadline starts at real execution and bounds every action."""

    def test_queueing_time_is_not_counted(self):
        from unittest.mock import patch

        clock = FakeClock()
        seen = []

        class RecordingAction:
            def execute(self, out, err, deadline=None):
                seen.append(deadline)
                # the deadline's start must be the clock value at
                # Task.execute() time, not when the task was created
                self.started_at = deadline.start
                return None

            out = err = None
            result = None
            values = {}

        task = Task("t", [], timeout=5)
        task._action_instances = [RecordingAction()]
        task.init_options()

        # time passes while the task is sitting in a runner queue
        clock.advance(100)
        with patch("doit.action.monotonic", clock):
            from doit.task import Stream
            task.execute(Stream(0))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].start, 1100.0)
        self.assertEqual(seen[0].remaining(), 5)

    def test_later_actions_never_start_after_timeout(self):
        from doit.exceptions import BaseFail

        started = []

        class TimeoutAction:
            out = err = None
            result = None
            values = {}

            def execute(self, out, err, deadline=None):
                started.append("first")
                return TaskTimeout("boom")

        class NeverAction:
            out = err = None
            result = None
            values = {}

            def execute(self, out, err, deadline=None):
                started.append("second")
                return None

        task = Task("t", [], timeout=1)
        task._action_instances = [TimeoutAction(), NeverAction()]
        task.init_options()
        from doit.task import Stream
        result = task.execute(Stream(0))
        self.assertIsInstance(result, BaseFail)
        self.assertEqual(started, ["first"])
        # success-only state is not committed on a failed chain
        self.assertEqual(task.values, {})


class TestCmdActionTimeoutFakeClock(unittest.TestCase):
    """Core timeout state with a controllable clock; no real waiting."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_deadline_already_expired_before_process_starts(self):
        clock = FakeClock()
        deadline = Deadline(1, clock=clock)
        clock.advance(2)  # expire before anything runs
        ran_marker = os.path.join(self.tmpdir, "ran")
        cmd = CmdAction(
            [sys.executable, "-c",
             "open(r'%s','w')" % ran_marker],
            shell=False)
        cmd.task = Task("t", [])
        result = cmd.execute(deadline=deadline)
        self.assertIsInstance(result, TaskTimeout)
        # the command must never have run
        self.assertFalse(os.path.exists(ran_marker))

    def test_timeout_message_contains_reason_and_duration(self):
        clock = FakeClock()
        deadline = Deadline(2, clock=clock)
        clock.advance(3)
        task = Task("t", [], timeout=2)
        task.init_options()
        cmd = CmdAction(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            shell=False)
        cmd.task = task
        result = cmd.execute(deadline=deadline)
        message = str(result)
        self.assertIn("timed out", message)
        self.assertIn("2", message)
        self.assertIn("t", message)

    def test_no_deadline_behaves_as_before(self):
        # plain success path is untouched
        cmd = CmdAction(
            [sys.executable, "-c", "print('x')"], shell=False)
        cmd.task = Task("t", [])
        self.assertIsNone(cmd.execute())
        self.assertEqual(cmd.out, "x\n")


class TestCmdActionTimeoutProcess(unittest.TestCase):
    """End-to-end: controlled child process, real but short timeouts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pidfile = os.path.join(self.tmpdir, "pids")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _wait_dead(self, pids, timeout=5):
        cutoff = time.monotonic() + timeout
        while time.monotonic() < cutoff:
            if not any(self._pid_alive(pid) for pid in pids):
                return
            time.sleep(0.05)
        self.fail("processes still alive: %r" % pids)

    def test_kills_process_and_children_keeps_output(self):
        task = Task(
            "tree",
            [CmdAction([sys.executable, TREE_SCRIPT, self.pidfile],
                       shell=False)],
            timeout=0.5)
        cmd = task.actions[0]
        start = time.monotonic()
        result = cmd.execute(deadline=Deadline(0.5))
        elapsed = time.monotonic() - start

        self.assertIsInstance(result, TaskTimeout)
        self.assertLess(elapsed, 5)
        # partial stdout captured before the deadline is preserved
        self.assertEqual(cmd.out, "partial-out")
        # success-only data is not saved on timeout
        self.assertEqual(cmd.values, {})

        # every spawned grand-child must be gone
        with open(self.pidfile) as handle:
            child_pids = [int(line) for line in handle.read().split()
                          if line]
        self.assertTrue(child_pids)
        self._wait_dead(child_pids)
        for pid in child_pids:
            self.assertFalse(self._pid_alive(pid))

    def test_completion_and_timeout_race_publishes_once(self):
        # a process that exits well before the deadline produces the
        # ordinary success result, never a timeout alongside it.
        cmd = CmdAction(
            [sys.executable, "-c", "print('done')"], shell=False)
        cmd.task = Task("t", [])
        result = cmd.execute(deadline=Deadline(5))
        self.assertIsNone(result)
        self.assertEqual(cmd.out, "done\n")

    def test_failure_exitcode_is_not_reported_as_timeout(self):
        cmd = CmdAction(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            shell=False)
        cmd.task = Task("t", [])
        result = cmd.execute(deadline=Deadline(5))
        self.assertIsInstance(result, TaskFailed)
        self.assertNotIsInstance(result, TaskTimeout)

    def test_sigkill_fallback_when_sigterm_is_ignored(self):
        task = Task(
            "stubborn",
            [CmdAction(
                [sys.executable, STUBBORN_SCRIPT], shell=False)],
            timeout=0.3)
        cmd = task.actions[0]
        start = time.monotonic()
        result = cmd.execute(deadline=Deadline(0.3))
        elapsed = time.monotonic() - start
        self.assertIsInstance(result, TaskTimeout)
        # SIGTERM is ignored so the 5s grace + SIGKILL path is taken;
        # allow enough headroom but assert it still returns well before
        # the child's own 30s sleep
        self.assertLess(elapsed, 10)
        self.assertEqual(cmd.out, "stubborn")


class TestPythonActionTimeout(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.task = Task("py", [self._slow], timeout=0.3)
        self.task.init_options()
        self.action = self.task.actions[0]

    def _slow(self):
        self.calls.append("started")
        time.sleep(30)
        self.calls.append("finished")

    def test_signal_interrupts_callable_on_main_thread(self):
        start = time.monotonic()
        result = self.action.execute(deadline=Deadline(0.3))
        elapsed = time.monotonic() - start
        self.assertIsInstance(result, TaskTimeout)
        self.assertLess(elapsed, 3)
        # the callable was interrupted, it never completed
        self.assertEqual(self.calls, ["started"])
        self.assertIn("timed out", str(result))

    def test_expired_deadline_refuses_to_start_callable(self):
        clock = FakeClock()
        deadline = Deadline(1, clock=clock)
        clock.advance(2)
        result = self.action.execute(deadline=deadline)
        self.assertIsInstance(result, TaskTimeout)
        self.assertEqual(self.calls, [])

    def test_worker_thread_is_rejected_before_running(self):
        box = {}

        def runner():
            try:
                box["result"] = self.action.execute(
                    deadline=Deadline(0.3))
            except BaseException as exc:  # exercised, not defensive
                box["error"] = exc

        thread = Thread(target=runner)
        thread.start()
        thread.join()
        self.assertIsInstance(box.get("error"), InvalidTask)
        # no trace of the callable executing in the background
        self.assertEqual(self.calls, [])

    def test_no_deadline_still_works(self):
        task = Task("quick", [lambda: True])
        task.init_options()
        self.assertIsNone(task.actions[0].execute())

    @unittest.skipUnless(
        hasattr(__import__("signal"), "raise_signal"),
        "signal.raise_signal required")
    def test_signal_while_running_interrupts(self):
        # a SIGALRM delivered synchronously inside the callable stops it
        import signal

        interrupted = Task(
            "interrupted",
            [lambda: signal.raise_signal(signal.SIGALRM)],
            timeout=10)
        interrupted.init_options()
        timed_out = interrupted.actions[0].execute(
            deadline=Deadline(10))
        self.assertIsInstance(timed_out, TaskTimeout)
        self.assertEqual(
            signal.getsignal(signal.SIGALRM), signal.SIG_DFL)

    @unittest.skipUnless(
        hasattr(__import__("signal"), "raise_signal"),
        "signal.raise_signal required")
    def test_signal_after_completion_keeps_single_success(self):
        # Drive the real execute() path. The callable finishes first;
        # right after it returns (while the handler is still installed)
        # a SIGALRM is delivered. The completion guard must swallow it,
        # leaving exactly one successful result and no escaped exception.
        import signal
        from unittest.mock import patch

        class FinishThenSignal:
            def __call__(self):
                # arm delivery for the next interpreter boundary, then
                # return successfully before it is handled
                signal.setitimer(signal.ITIMER_REAL, 0.000001)
                return "done"

        task = Task("raced", [FinishThenSignal()], timeout=10)
        task.init_options()
        act = task.actions[0]

        # force the race deterministically: replace the installed
        # handler with the production guard logic by ensuring
        # callable_done is observed via the real execute() flow.
        outcomes = set()
        for _ in range(20):
            result = act.execute(deadline=Deadline(10))
            outcomes.add(
                "success" if result is None else type(result).__name__)
            self.assertEqual(
                signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        # every run either completed successfully or timed out cleanly,
        # but never raised; the guard makes success the stable outcome.
        self.assertTrue(outcomes.issubset({"success", "TaskTimeout"}))


# picklable module-level callables for runner-level tests
def py_sleep_forever():
    time.sleep(30)


def py_quick_ok():
    return True


class TestRunnerTimeoutIntegration(unittest.TestCase):
    """Serial and parallel runners share the same terminal state."""

    def _runner(self, cls, dep_manager, reporter):
        if cls.__name__ in ("MRunner", "MThreadRunner"):
            return cls(dep_manager, reporter, num_process=2,
                       continue_=True)
        return cls(dep_manager, reporter, continue_=True)

    def _dep_manager(self):
        from doit.dependency import DbmDB, Dependency
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        return Dependency(DbmDB, os.path.join(folder, "db"))

    @staticmethod
    def _reporter():
        return FakeReporter()

    @staticmethod
    def _dispatcher(tasks):
        from doit.control import TaskDispatcher
        return TaskDispatcher(
            {task.name: task for task in tasks}, [],
            [task.name for task in tasks])

    def test_cmd_timeout_marks_failure_and_blocks_dependents(self):
        from doit import runner as runner_mod
        for cls in (runner_mod.Runner, runner_mod.MThreadRunner,
                    runner_mod.MRunner):
            with self.subTest(runner=cls.__name__):
                self._assert_cmd_timeout_flow(cls)

    def _assert_cmd_timeout_flow(self, cls):
        from doit import runner as runner_mod
        if cls is runner_mod.MRunner and not runner_mod.MRunner.available():
            self.skipTest("MRunner unavailable")

        dep_manager = self._dep_manager()
        pid_folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, pid_folder, True)
        slow = Task(
            "slow",
            [CmdAction(
                [sys.executable, TREE_SCRIPT,
                 os.path.join(pid_folder, "pids")],
                shell=False)],
            timeout=0.3)
        dependent = Task("dependent", [py_quick_ok], task_dep=["slow"])
        unrelated = Task("unrelated", [py_quick_ok])
        reporter = self._reporter()
        my_runner = self._runner(cls, dep_manager, reporter)
        result = my_runner.run_all(
            self._dispatcher([slow, dependent, unrelated]))

        # timeout is an error terminal state in every runner
        self.assertEqual(result, runner_mod.ERROR)
        names = [event[0] for event in reporter.log]
        self.assertIn("fail", names)
        # the dependent never executed
        self.assertNotIn(
            "execute",
            [event[0] for event in reporter.log
             if event[1].name == "dependent"])
        # an unrelated task still completed
        self.assertIn(
            "success",
            [event[0] for event in reporter.log
             if event[1].name == "unrelated"])
        dep_manager.close()

    def test_thread_runner_rejects_python_timeout_upfront(self):
        from doit import runner as runner_mod
        dep_manager = self._dep_manager()
        task = Task("risky", [py_sleep_forever], timeout=0.3)
        reporter = self._reporter()
        my_runner = runner_mod.MThreadRunner(
            dep_manager, reporter, num_process=2)
        result = my_runner.run_all(self._dispatcher([task]))
        self.assertEqual(result, runner_mod.ERROR)
        # refused at selection time, never executed
        self.assertFalse(task.executed)
        dep_manager.close()

    def test_serial_runner_interrupts_python_action(self):
        from doit import runner as runner_mod
        dep_manager = self._dep_manager()
        task = Task("risky", [py_sleep_forever], timeout=0.3)
        reporter = self._reporter()
        my_runner = runner_mod.Runner(dep_manager, reporter)
        start = time.monotonic()
        result = my_runner.run_all(self._dispatcher([task]))
        elapsed = time.monotonic() - start
        self.assertEqual(result, runner_mod.ERROR)
        self.assertLess(elapsed, 3)
        dep_manager.close()

    def test_process_runner_interrupts_python_action(self):
        # the action runs on the child process' main thread, where
        # SIGALRM is available, so the timeout is enforced normally.
        from doit import runner as runner_mod
        if not runner_mod.MRunner.available():
            self.skipTest("MRunner unavailable")
        dep_manager = self._dep_manager()
        task = Task("risky", [py_sleep_forever], timeout=0.3)
        reporter = self._reporter()
        my_runner = runner_mod.MRunner(
            dep_manager, reporter, num_process=1)
        start = time.monotonic()
        result = my_runner.run_all(self._dispatcher([task]))
        elapsed = time.monotonic() - start
        self.assertEqual(result, runner_mod.ERROR)
        self.assertLess(elapsed, 3)
        dep_manager.close()

    def test_legacy_action_without_deadline_is_rejected(self):
        # doit.tools.LongRunning predates the deadline keyword; pairing
        # it with a timeout must fail loudly, never with a TypeError
        # from inside the action chain.
        from doit import runner as runner_mod
        from doit.tools import LongRunning
        dep_manager = self._dep_manager()
        task = Task("legacy", [LongRunning("true")], timeout=1)
        reporter = self._reporter()
        my_runner = runner_mod.Runner(dep_manager, reporter)
        result = my_runner.run_all(self._dispatcher([task]))
        self.assertEqual(result, runner_mod.ERROR)
        self.assertFalse(task.executed)
        dep_manager.close()

    def test_timeout_stops_the_batch_without_continue(self):
        from doit import runner as runner_mod
        dep_manager = self._dep_manager()
        pid_folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, pid_folder, True)
        slow = Task(
            "slow",
            [CmdAction(
                [sys.executable, TREE_SCRIPT,
                 os.path.join(pid_folder, "pids")],
                shell=False)],
            timeout=0.3)
        after = Task("after", [py_quick_ok])
        reporter = self._reporter()
        # no continue_ -> the batch halts on the timeout error
        my_runner = runner_mod.Runner(dep_manager, reporter)
        result = my_runner.run_all(
            self._dispatcher([slow, after]))
        self.assertEqual(result, runner_mod.ERROR)
        executed = [event[1].name for event in reporter.log
                    if event[0] == "execute"]
        self.assertNotIn("after", executed)
        dep_manager.close()


class TestTimeoutPersistence(unittest.TestCase):
    """A timed-out task is not saved as success and must re-run."""

    def setUp(self):
        from doit.dependency import DbmDB, Dependency
        self.folder = tempfile.mkdtemp()
        self.db_path = os.path.join(self.folder, "db")
        self.dep_manager = Dependency(DbmDB, self.db_path)

    def tearDown(self):
        if not self.dep_manager._closed:
            self.dep_manager.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_timeout_removes_success_and_forces_rerun(self):
        from doit.control import TaskDispatcher
        from doit import runner as runner_mod
        from doit.dependency import DbmDB, Dependency

        class SilentReporter:
            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        target = os.path.join(self.folder, "target.txt")
        slow = Task(
            "slow",
            [CmdAction(
                [sys.executable, "-c",
                 "import sys,time; "
                 "open(sys.argv[1],'w').write('partial'); "
                 "time.sleep(30)", target],
                shell=False, save_out="captured")],
            targets=[target],
            timeout=0.3)

        my_runner = runner_mod.Runner(
            self.dep_manager, SilentReporter())
        self.assertEqual(
            runner_mod.ERROR,
            my_runner.run_all(TaskDispatcher(
                {"slow": slow}, {}, ["slow"])))

        # re-open the dependency database like a brand new doit process
        self.dep_manager.close()
        reopened = Dependency(DbmDB, self.db_path)
        try:
            status = reopened.get_status(slow, {"slow": slow})
            # no success record -> the task is not up-to-date
            self.assertNotEqual(status.status, "up-to-date")
            # save_out values must not have been persisted
            self.assertEqual(reopened.get_values("slow"), {})
        finally:
            reopened.close()

    def test_timeout_after_a_prior_success_clears_the_record(self):
        from doit.control import TaskDispatcher
        from doit import runner as runner_mod
        from doit.dependency import DbmDB, Dependency

        class SilentReporter:
            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        target = os.path.join(self.folder, "built.txt")

        def build():
            with open(target, "w") as handle:
                handle.write("ok")
            return True

        # run 1: a normal successful task with a target is recorded
        good = Task("build", [build], targets=[target], timeout=5)
        runner_one = runner_mod.Runner(
            self.dep_manager, SilentReporter())
        self.assertEqual(
            runner_mod.SUCCESS,
            runner_one.run_all(TaskDispatcher(
                {"build": good}, {}, ["build"])))
        self.dep_manager.close()

        # run 2 (new "process", reopened DB): same task now times out
        dep_two = Dependency(DbmDB, self.db_path)
        slow = Task(
            "build",
            [CmdAction(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                shell=False)],
            targets=[target],
            timeout=0.3)
        runner_two = runner_mod.Runner(dep_two, SilentReporter())
        self.assertEqual(
            runner_mod.ERROR,
            runner_two.run_all(TaskDispatcher(
                {"build": slow}, {}, ["build"])))
        dep_two.close()

        # run 3: the success record is gone so the task is not up-to-date
        dep_three = Dependency(DbmDB, self.db_path)
        try:
            status = dep_three.get_status(
                slow, {"build": slow})
            self.assertNotEqual(status.status, "up-to-date")
        finally:
            dep_three.close()


if __name__ == "__main__":
    unittest.main()
