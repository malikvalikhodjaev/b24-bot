import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('miniapp_installer', ROOT / 'tools/install_miniapp.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_patch_is_additive_and_idempotent(self):
        source = (ROOT.parent / 'outputs/fom-pharma-portfolio-work/scripts/dashboard_server.py').read_text(encoding='utf-8')
        original = source.replace(installer.METHOD, '', 1).replace('\n        if self.serve_bot_form(request_path):\n            return\n', '')
        updated = installer.patch_source(original)
        self.assertEqual(updated, source)
        self.assertEqual(installer.patch_source(updated), updated)
        self.assertIn('if not self.ensure_authorized():', updated)
        self.assertEqual(original.count('if not self.ensure_authorized():'), updated.count('if not self.ensure_authorized():'))

    def test_changed_or_ambiguous_handler_is_rejected(self):
        with self.assertRaises(RuntimeError): installer.patch_source('class Example: pass\n')
        with self.assertRaises(RuntimeError): installer.patch_source('    def serve_bot_form(self, path): pass\n')

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(RuntimeError): installer.safe_path(ROOT, '../other-project/file.py')


if __name__ == '__main__': unittest.main()
