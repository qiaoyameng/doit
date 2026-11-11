"""Per-task environment variable declarations.

A task may declare an ``env`` dict describing the environment its actions
should run with.  The declaration supports:

* inheriting the parent process environment (default),
* overriding variables with plain strings,
* setting variables whose value must never be logged or persisted
  (``SensitiveValue``), and
* explicitly removing inherited variables (value ``None``).

A declaration is resolved into an :class:`EnvSnapshot` once, when the task
starts executing.  All actions of the task use that fixed snapshot, so the
environment does not change in the middle of a task run.

``CmdAction`` always spawns a new process, so it simply passes the resolved
environment to ``subprocess``.  ``PythonAction`` runs a callable in-process
and can only provide the declared environment by mutating ``os.environ``
while it runs.  That is safe only when no other task may execute
concurrently in the same process (the default serial ``Runner`` and the
multiprocessing ``MRunner``); the threaded runner refuses to execute such
actions instead of silently polluting sibling tasks.
"""

import os
import hashlib

from .exceptions import InvalidTask, TaskError


# value used on env declarations to explicitly remove an inherited variable
DELETE = None


class SensitiveValue:
    """Wrapper for environment values that must be kept secret.

    The plain text is used to build the environment of the action's
    process, but it is never written to logs nor to the dependency DB:
    only the variable name and an irreversible fingerprint (SHA-256) of
    the value are kept.
    """

    __slots__ = ('_value',)

    def __init__(self, value):
        if not isinstance(value, str):
            msg = "SensitiveValue must be a 'str' got %r."
            raise InvalidTask(msg % (value,))
        self._value = value

    @property
    def value(self):
        """plain text value, use with care"""
        return self._value

    def fingerprint(self):
        """irreversible digest of the plain text value"""
        return hashlib.sha256(self._value.encode('utf-8')).hexdigest()

    def __repr__(self):
        return "SensitiveValue(***)"

    __str__ = __repr__


class TaskEnvSpec:
    """Parsed and validated ``env`` task attribute.

    :ivar dict overrides: var-name -> str value or SensitiveValue
    :ivar set deletions: names of variables to remove from parent env
    """

    def __init__(self, env, task_name):
        if not isinstance(env, dict):
            msg = "Task '%s' attribute 'env' must be a dict. got:%r %s"
            raise InvalidTask(msg % (task_name, env, type(env)))
        self.overrides = {}
        self.deletions = set()
        for name, value in env.items():
            if not isinstance(name, str):
                msg = ("Task '%s' attribute 'env' keys must be str. "
                       "got:%r %s")
                raise InvalidTask(msg % (task_name, name, type(name)))
            if value is DELETE:
                self.deletions.add(name)
            elif isinstance(value, (str, SensitiveValue)):
                self.overrides[name] = value
            else:
                msg = ("Task '%s' attribute 'env' values must be str, "
                       "None (delete variable) or SensitiveValue. "
                       "got:%r %s")
                raise InvalidTask(msg % (task_name, value, type(value)))

    def resolve(self, base_env=None):
        """create an EnvSnapshot applying this spec to `base_env`"""
        return EnvSnapshot(self, base_env)

    def variable_fingerprints(self, names=None):
        """map variable name -> irreversible fingerprint for the declaration

        Only field names and SHA-256 digests are returned, plain text
        sensitive values are never included.  A deleted variable maps to
        ``None``.  `names` restricts the result to declared variables.
        """
        if names is None:
            selected = list(self.deletions) + list(self.overrides)
        else:
            selected = [name for name in names
                        if name in self.deletions or name in self.overrides]
        result = {}
        for name in sorted(selected):
            if name in self.deletions:
                result[name] = None
            else:
                value = self.overrides[name]
                if isinstance(value, SensitiveValue):
                    result[name] = value.fingerprint()
                else:
                    result[name] = hashlib.sha256(
                        value.encode('utf-8')).hexdigest()
        return result

    def fingerprint(self, base_env=None):
        """digest of the effective environment declared by this spec"""
        if base_env is None:
            base_env = os.environ
        digest = hashlib.sha256()
        for name, value_digest in self.variable_fingerprints().items():
            digest.update(b'\0' + name.encode('utf-8') + b'\0' +
                          (value_digest or 'deleted').encode('utf-8'))
        return digest.hexdigest()


class EnvSnapshot:
    """Fixed environment to be used by all actions of a single task run.

    Created once when the task starts executing, so concurrent changes of
    the parent process environment do not affect a task in flight.
    """

    def __init__(self, spec, base_env=None):
        if base_env is None:
            base_env = os.environ
        # start from a copy of the parent environment (inherit)
        self._env = dict(base_env)
        for name in spec.deletions:
            self._env.pop(name, None)
        for name, value in spec.overrides.items():
            if isinstance(value, SensitiveValue):
                self._env[name] = value.value
            else:
                self._env[name] = value
        self._fingerprint = spec.fingerprint(base_env)

    @property
    def environ(self):
        """dict with the resolved environment (contains plain values)"""
        return dict(self._env)

    def fingerprint(self):
        """digest of this snapshot, stable for the whole task run"""
        return self._fingerprint

    def __repr__(self):
        return "<EnvSnapshot: %s>" % self._fingerprint[:12]


def unsafe_env_action_msg(task_name, action):
    """error message used when a python-action env cannot be provided safely"""
    return (
        "Task '%s': python-action %r declares a task 'env' but the current "
        "execution mode runs tasks concurrently in this process, so the "
        "environment cannot be provided without polluting other tasks. "
        "Run the task in an isolated process (do not use "
        "'--parallel-type thread') or use a CmdAction instead."
        % (task_name, action))


def env_unsafe_error(task_name, action):
    """TaskError for python-actions whose env cannot be safely provided"""
    return TaskError(unsafe_env_action_msg(task_name, action))
