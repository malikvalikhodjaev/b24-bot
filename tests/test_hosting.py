import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datfo_crm_bot.config import ConfigError
from datfo_crm_bot.hosting import assert_polling_location


class HostingTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        (self.root/'data').mkdir()

    def tearDown(self):
        self.folder.cleanup()

    def test_legacy_local_install_without_migration_marker_is_allowed(self):
        with patch('datfo_crm_bot.hosting.ROOT',self.root),patch.dict(os.environ,{},clear=True):
            assert_polling_location()

    def test_migrated_laptop_cannot_start_a_second_poller(self):
        (self.root/'data/server-hosting.json').write_text(json.dumps({'active':True}),encoding='utf-8')
        with patch('datfo_crm_bot.hosting.ROOT',self.root),patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ConfigError,'Бот работает на сервере'):
                assert_polling_location()

    def test_server_service_can_run_with_its_explicit_execution_environment(self):
        (self.root/'data/server-hosting.json').write_text(json.dumps({'active':True}),encoding='utf-8')
        with patch('datfo_crm_bot.hosting.ROOT',self.root),patch.dict(os.environ,{'FOM_BOT_EXECUTION':'server'},clear=True):
            assert_polling_location()

    def test_invalid_migration_marker_does_not_silently_start_a_second_poller(self):
        (self.root/'data/server-hosting.json').write_text('broken',encoding='utf-8')
        with patch('datfo_crm_bot.hosting.ROOT',self.root),patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ConfigError,'Не удалось проверить место запуска'):
                assert_polling_location()


if __name__=='__main__':
    unittest.main()
