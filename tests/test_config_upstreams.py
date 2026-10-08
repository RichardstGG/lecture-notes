"""Private registry and public CLI/config contracts (no GPU required)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _pathfix  # noqa: F401
from core import config as C, cli


class UpstreamConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'upstreams.toml'
        p = patch.object(C, 'UPSTREAMS_FILE', self.path)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = C.Config(C.load_toml(C.DEFAULT_FILE))

    def registry(self, **changes):
        spec = dict(name='內網 GPU', base_url='http://private.invalid:8000/v1',
                    model='private-model', api_key='secret-token')
        spec.update(changes)
        self.path.write_text(C.dump_toml({'upstreams': {'lab': spec}}), encoding='utf-8')

    def test_absent_registry_preserves_local(self):
        self.assertIsNone(self.cfg.remote_summary())
        self.assertEqual(self.cfg.upstream_inventory()['options'],
                         [{'id': 'local', 'name': '本地 GPU', 'kind': 'local'}])

    def test_private_values_do_not_enter_config_dump_or_inventory(self):
        self.registry()
        self.cfg.data['summary']['upstream'] = 'lab'
        url, model = self.cfg.remote_summary()
        self.assertEqual(model['model_id'], 'private-model')
        self.assertEqual(model['api_key'], 'secret-token')
        public = self.cfg.dump() + json.dumps(cli._model_inventory(self.cfg))
        for secret in ('private.invalid', 'private-model', 'secret-token', 'api_key', 'base_url'):
            self.assertNotIn(secret, public)
        self.assertEqual(self.cfg.upstream_inventory()['options'][1]['id'], 'lab')
        self.assertEqual(url, 'http://private.invalid:8000/v1')
        self.assertEqual(self.cfg.data['summary']['upstream'], 'lab')

    def test_env_auth_resolved_only_for_selected_request(self):
        self.path.write_text(C.dump_toml({'upstreams': {'lab': {
            'name': 'Lab', 'base_url': 'https://private.invalid/api/v1/',
            'model': '30b', 'api_key_env': 'LEC_TEST_KEY'}}}), encoding='utf-8')
        self.cfg.data['summary']['upstream'] = 'lab'
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(self.cfg.upstream_inventory()['options']), 2)
            with self.assertRaises(C.ConfigError):
                self.cfg.remote_summary()
        with patch.dict(os.environ, {'LEC_TEST_KEY': 'abc'}):
            url, model = self.cfg.remote_summary()
            self.assertEqual(model['api_key'], 'abc')
            self.assertTrue(url.endswith('/api/v1'))

    def test_invalid_and_unknown_registry_errors_never_echo_values(self):
        for spec in ({'base_url': 'http://user:secret@host/v1'},
                     {'base_url': 'https://host/v1?token=secret'},
                     {'base_url': 'file:///secret'}, {'model': ''},
                     {'api_key_env': 'SECRET'}, {'api_key': 'secret\r\n'}):
            with self.subTest(spec=spec):
                self.registry(**spec)
                with self.assertRaises(C.ConfigError) as error:
                    C.load_upstreams()
                self.assertNotIn('secret', str(error.exception))
        self.path.write_text('[upstreams.lab]\napi_key="secret', encoding='utf-8')
        with self.assertRaises(C.ConfigError) as error:
            C.load_upstreams()
        self.assertNotIn('secret', str(error.exception))
        self.path.unlink()
        self.cfg.data['summary']['upstream'] = 'missing'
        with self.assertRaises(C.ConfigError):
            self.cfg.remote_summary()

    def test_remote_validation_does_not_require_local_model(self):
        self.cfg.data['models'] = {}
        self.cfg.data['summary']['upstream'] = 'lab'
        self.assertEqual(self.cfg.validate(), [])
        self.cfg.data['summary']['request_timeout'] = float('inf')
        self.assertTrue(self.cfg.validate())
        self.cfg.data['summary']['upstream'] = 'local'
        self.assertTrue(any('summary.model' in e for e in self.cfg.validate()))

    def test_cli_upstream_overrides_course_for_run_and_redo(self):
        for command in ('run', 'summarize'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                course = root / 'course.toml'
                course.write_text('[summary]\nupstream="old"\n', encoding='utf-8')
                used = root / 'config.used.toml'
                used.write_text('[summary]\nupstream="old"\n', encoding='utf-8')
                cls = 'LectureRun' if command == 'run' else 'OfflineSummary'
                args = ['run', str(course)] if command == 'run' else ['summarize', tmp, '--redo', 'all']
                with patch.object(C, 'LOCAL_FILE', root / 'absent'), patch('core.session.' + cls) as run:
                    run.return_value.run.return_value = 0
                    self.assertEqual(cli.main(args + ['--upstream', 'lab']), 0)
                    self.assertEqual(run.call_args.args[0].get('summary.upstream'), 'lab')

    def test_models_cli_json_is_public_projection(self):
        self.registry()
        output = io.StringIO()
        with patch.object(C, 'load', return_value=(self.cfg, False)), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['models', '--json']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['summary_upstreams']['options'][1],
                         {'id': 'lab', 'name': '內網 GPU', 'kind': 'api'})
        self.assertNotIn('secret-token', output.getvalue())
