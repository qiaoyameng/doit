import os
import unittest

from doit.exceptions import InvalidTask
from doit.task_env import TaskEnvSpec, SensitiveValue, EnvSnapshot


class TestSensitiveValue(unittest.TestCase):
    def test_fingerprint_is_not_plain_text(self):
        sens = SensitiveValue('s3cr3t')
        self.assertNotIn('s3cr3t', repr(sens))
        self.assertNotIn('s3cr3t', str(sens))
        self.assertEqual(64, len(sens.fingerprint()))
        self.assertEqual('s3cr3t', sens.value)

    def test_fingerprint_stable(self):
        self.assertEqual(SensitiveValue('abc').fingerprint(),
                         SensitiveValue('abc').fingerprint())
        self.assertNotEqual(SensitiveValue('abc').fingerprint(),
                            SensitiveValue('abd').fingerprint())

    def test_invalid_type(self):
        self.assertRaises(InvalidTask, SensitiveValue, 123)


class TestTaskEnvSpec(unittest.TestCase):
    def test_parse(self):
        spec = TaskEnvSpec({'TZ': 'UTC', 'OLD': None,
                            'TOKEN': SensitiveValue('x')}, 't1')
        self.assertEqual({'TZ': 'UTC'}, {k: v for k, v in spec.overrides.items()
                                         if isinstance(v, str)})
        self.assertIsInstance(spec.overrides['TOKEN'], SensitiveValue)
        self.assertEqual({'OLD'}, spec.deletions)

    def test_invalid_not_dict(self):
        self.assertRaises(InvalidTask, TaskEnvSpec, ['TZ=UTC'], 't1')

    def test_invalid_key(self):
        self.assertRaises(InvalidTask, TaskEnvSpec, {1: 'x'}, 't1')

    def test_invalid_value(self):
        self.assertRaises(InvalidTask, TaskEnvSpec, {'TZ': 5}, 't1')


class TestEnvSnapshot(unittest.TestCase):
    def test_inherit_override_delete(self):
        base = {'KEEP': 'k', 'OVER': 'old', 'DEL': 'gone'}
        spec = TaskEnvSpec({'OVER': 'new', 'DEL': None, 'ADD': 'a'}, 't1')
        snap = spec.resolve(base)
        self.assertEqual({'KEEP': 'k', 'OVER': 'new', 'ADD': 'a'},
                         snap.environ)

    def test_snapshot_is_fixed(self):
        # later changes on the base env do not affect the snapshot
        base = {'VAR': 'one'}
        spec = TaskEnvSpec({}, 't1')
        snap = spec.resolve(base)
        base['VAR'] = 'two'
        base['NEW'] = 'n'
        self.assertEqual({'VAR': 'one'}, snap.environ)

    def test_environ_returns_copy(self):
        spec = TaskEnvSpec({'A': '1'}, 't1')
        snap = spec.resolve({})
        env = snap.environ
        env['A'] = 'mutated'
        self.assertEqual('1', snap.environ['A'])

    def test_sensitive_value_resolved_in_snapshot(self):
        spec = TaskEnvSpec({'TOKEN': SensitiveValue('plain')}, 't1')
        snap = spec.resolve({})
        self.assertEqual('plain', snap.environ['TOKEN'])

    def test_fingerprint_no_plain_sensitive_value(self):
        spec = TaskEnvSpec({'TOKEN': SensitiveValue('super-secret')}, 't1')
        snap = spec.resolve({})
        # fingerprint is a hex digest, never contains the plain value
        self.assertNotIn('super-secret', snap.fingerprint())
        self.assertEqual(snap.fingerprint(), spec.fingerprint({}))

    def test_fingerprint_changes_with_declaration(self):
        base = {}
        spec_a = TaskEnvSpec({'TZ': 'UTC'}, 't1')
        spec_b = TaskEnvSpec({'TZ': 'CET'}, 't1')
        spec_c = TaskEnvSpec({'TZ': 'UTC', 'X': None}, 't1')
        self.assertNotEqual(spec_a.fingerprint(base), spec_b.fingerprint(base))
        self.assertNotEqual(spec_a.fingerprint(base), spec_c.fingerprint(base))
        # stable
        self.assertEqual(spec_a.fingerprint(base), spec_a.fingerprint(base))

    def test_default_base_is_os_environ(self):
        spec = TaskEnvSpec({'DOIT_TENV_X': '1'}, 't1')
        snap = spec.resolve()
        self.assertEqual('1', snap.environ['DOIT_TENV_X'])
        self.assertEqual(os.environ.get('PATH'), snap.environ.get('PATH'))


class TestVariableFingerprints(unittest.TestCase):
    def test_names_and_fingerprints(self):
        spec = TaskEnvSpec({'TZ': 'UTC', 'OLD': None,
                            'TOKEN': SensitiveValue('plain')}, 't1')
        result = spec.variable_fingerprints()
        self.assertEqual({'OLD': None, 'TOKEN': result['TOKEN'],
                          'TZ': result['TZ']}, result)
        self.assertIsNone(result['OLD'])
        self.assertEqual(64, len(result['TOKEN']))
        self.assertEqual(64, len(result['TZ']))

    def test_selected_names(self):
        spec = TaskEnvSpec({'TZ': 'UTC', 'LC_ALL': 'C'}, 't1')
        self.assertEqual({'LC_ALL'}, set(spec.variable_fingerprints(['LC_ALL'])))
        # names not declared are ignored
        self.assertEqual({}, spec.variable_fingerprints(['NOT_DECLARED']))

    def test_no_plain_values(self):
        import json
        spec = TaskEnvSpec({'TOKEN': SensitiveValue('super-secret'),
                            'TZ': 'UTC'}, 't1')
        self.assertNotIn('super-secret', json.dumps(
            spec.variable_fingerprints()))
        self.assertNotIn('UTC', json.dumps(spec.variable_fingerprints()))
