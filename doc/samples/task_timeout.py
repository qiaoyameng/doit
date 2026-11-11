import datetime


def task_slow_cleanup():
    # abort the external command if it runs for more than 5 seconds.
    # the clock starts when the task starts running (not when it is
    # queued). the whole action chain shares one deadline.
    return {'actions': ['some-slow-cleanup-command'],
            'timeout': 5}


def task_report():
    # timedelta is accepted too
    return {'actions': ['generate-report'],
            'timeout': datetime.timedelta(minutes=10)}
