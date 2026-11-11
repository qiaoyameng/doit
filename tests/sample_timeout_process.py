#! /usr/bin/env python3

"""Helper process used by task timeout tests.

Modes:
- block <ready_file>: write stdout marker, touch <ready_file>, then
  sleep forever (until killed). Used to trigger a CmdAction timeout.
- child <ready_file>: spawn a grand-child in a blocking wait, touch
  <ready_file> and block. Used to verify the whole process group
  (including children) is cleaned up on timeout.
- quick <marker>: print marker on stdout and exit successfully.
"""

import os
import sys
import time


def main():
    mode = sys.argv[1]
    if mode == 'quick':
        sys.stdout.write(sys.argv[2])
        sys.stdout.flush()
        return
    ready_file = sys.argv[2]
    child_pid = None
    if mode == 'child':
        child_pid = os.fork()
        if child_pid == 0:
            while True:
                time.sleep(60)
    sys.stdout.write("before-timeout\n")
    sys.stderr.write("err-before-timeout\n")
    sys.stdout.flush()
    sys.stderr.flush()
    with open(ready_file, 'w') as fh:
        fh.write(str(os.getpid()))
        if child_pid:
            fh.write(":%s" % child_pid)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
