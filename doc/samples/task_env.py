import os

from doit.task_env import SensitiveValue


def show_env():
    print("TZ=%s LC_ALL=%s" % (os.environ.get('TZ'),
                               os.environ.get('LC_ALL')))


def task_etl_utc():
    """runs with its own timezone/locale, parent process is not modified"""
    return {
        'actions': [show_env],
        'env': {'TZ': 'UTC', 'LC_ALL': 'C'},
        'verbosity': 2,
    }


def task_etl_ny():
    """overrides, deletes and keeps a secret out of logs and the DB"""
    return {
        'actions': ['echo "tz is $TZ, token set: ${TOKEN:+yes}"'],
        'env': {
            'TZ': 'America/New_York',   # override
            'LC_ALL': None,             # remove inherited variable
            'TOKEN': SensitiveValue('s3cr3t'),  # never logged nor persisted
        },
        'verbosity': 2,
    }
