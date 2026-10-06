"""Capture selection contract; these tests never open an audio device."""
import unittest
from unittest.mock import patch
from tests import _pathfix  # noqa: F401
from core import config as C


class CaptureConfigTests(unittest.TestCase):
    def config(self, **audio):
        data = C.load_toml(C.DEFAULT_FILE)
        data['audio'].update(audio)
        return C.Config(data)

    def test_legacy_default_and_alias_are_unchanged(self):
        cfg = self.config(source='desk', sources={'desk': {'pulse': 'alsa_input.usb'}})
        del cfg.data['audio']['capture_mode']
        self.assertEqual(cfg.capture_plan(), {'mode': 'single', 'tracks': []})
        self.assertEqual(cfg.audio_source(), 'alsa_input.usb')
        self.assertEqual(cfg.validate(), [])

    def test_dual_plan_has_stable_roles_and_distinct_device_ids(self):
        cfg = self.config(capture_mode='dual', system_source='alsa_output.usb.monitor',
                          microphone_source='alsa_input.usb')
        self.assertEqual(cfg.capture_plan(), {'mode': 'dual', 'tracks': [
            {'id': 'system', 'role': 'system', 'device_id': 'alsa_output.usb.monitor'},
            {'id': 'microphone', 'role': 'microphone', 'device_id': 'alsa_input.usb'}]})
        self.assertEqual(cfg.validate(), [])
        with self.assertRaisesRegex(C.ConfigError, 'capture_unavailable'):
            cfg.audio_source()

    def test_bad_missing_duplicate_and_alias_sources(self):
        for over in ({'capture_mode': 'other'}, {'sources': None}, {'sources': []}, {'system_source': ''},
                     {'microphone_source': None}, {'microphone_source': True},
                     {'microphone_source': 'default'}, {'microphone_source': '3'},
                     {'system_source': 'auto'}, {'system_source': ' bad'},
                     {'system_source': 'bad\nname'}, {'system_source': '../file'},
                     {'microphone_source': 'out.monitor'}, {'backend': 'dshow'},
                     {'sources': {'out.monitor': {'pulse': 'different'}}}):
            with self.subTest(over=over):
                args = dict(capture_mode='dual', system_source='out.monitor', microphone_source='mic')
                args.update(over)
                cfg = self.config(**args)
                with self.assertRaises(C.ConfigError):
                    cfg.capture_plan()
                self.assertTrue(cfg.validate())

    def test_cli_source_does_not_retarget_dual_roles(self):
        with patch.object(C.LOCAL_FILE.__class__, 'exists', return_value=False):
            cfg, _ = C.load(sets=['audio.capture_mode=dual', 'audio.system_source=out.monitor',
                                  'audio.microphone_source=mic'], source='legacy')
        self.assertEqual(cfg.get('audio.source'), 'legacy')
        self.assertEqual(cfg.capture_plan()['tracks'][1]['device_id'], 'mic')
