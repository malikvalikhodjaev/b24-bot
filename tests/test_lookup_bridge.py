import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('lookup_bridge',Path(__file__).resolve().parents[1]/'tools/install_lookup_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_lookup_precedes_dashboard_auth_and_other_paths_keep_it(self):
        original = ('class Handler:\n    def do_POST(self) -> None:\n        request_path = urlsplit(self.path).path\n'
                    '        if not self.ensure_authorized():\n            return\n')
        patched = bridge.patch_source(original)
        self.assertLess(patched.index("request_path == '/bot-form/api/lookup'"),patched.index('if not self.ensure_authorized()'))
        self.assertEqual(bridge.patch_source(patched),patched)
        self.assertIn("'http://127.0.0.1:18772/lookup'",patched)
        self.assertIn('<= 2048',patched)
        self.assertIn('https://fom-analytics.uz',patched)
        self.assertNotIn('Access-Control-Allow-Origin',patched)

    def test_changed_route_is_rejected_instead_of_overwritten(self):
        with self.assertRaises(RuntimeError):
            bridge.patch_source('class Handler:\n    def serve_bot_lookup(self):\n        pass\n')
        with self.assertRaises(RuntimeError):
            bridge.patch_source('class Handler:\n    def do_POST(self):\n        pass\n')
