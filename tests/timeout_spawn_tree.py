"""Controlled subprocess for task-timeout tests.

Spawns two child processes, records their PIDs in ``<pidfile>`` (one per
line), prints a partial stdout marker, then sleeps. Tests kill the parent
after a timeout and verify every recorded PID has exited.
"""
import subprocess
import sys
import time


def main():
    pidfile = sys.argv[1]
    child_pids = []
    for _ in range(2):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=False)
        child_pids.append(child.pid)
    with open(pidfile, "w") as handle:
        handle.write("\n".join(str(pid) for pid in child_pids))
    sys.stdout.write("partial-out")
    sys.stdout.flush()
    time.sleep(30)


if __name__ == "__main__":
    main()
