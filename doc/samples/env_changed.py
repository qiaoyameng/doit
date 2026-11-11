from doit.tools import env_changed


def task_etl_a():
    """re-executed only when its declared environment changes"""
    return {
        'actions': ['echo "extracting with TZ=$TZ"'],
        'env': {'TZ': 'UTC'},
        'uptodate': [env_changed()],
    }


def task_etl_b():
    """only the selected variables participate in the up-to-date check"""
    return {
        'actions': ['echo "loading with LC_ALL=$LC_ALL"'],
        'env': {'LC_ALL': 'C', 'TZ': 'UTC'},
        'uptodate': [env_changed(['LC_ALL'])],
    }
