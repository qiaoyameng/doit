"""Controlled subprocess that ignores SIGTERM for the SIGKILL fallback.

Used to prove a timeout still reaps a process that refuses to terminate
gracefully.
"""
import signal
import sys
import time


def main():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.write("stubborn")
    sys.stdout.flush()
    time.sleep(30)


if __name__ == "__main__":
    main()
