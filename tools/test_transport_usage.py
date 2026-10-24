import unittest
from pathlib import Path
from transport_usage import uses_api, usage
import runtime_config as runtime

class TransportUsageTest(unittest.TestCase):
    def test_annotation_import_and_fully_qualified_usage_require_policy_owner(self):
        api = 'com.raxim.myscoutee.domain.access.api.RequiresPolicy'
        for source in [f'import {api}; @RequiresPolicy(key="media.read") public void read() {{}}',
                       f'@{api}(key="media.read") public void read() {{}}']:
            self.assertTrue(uses_api(source, api))

    def test_import_comments_and_nested_dto_do_not_grant_capability(self):
        api = 'com.example.Owner'
        for source in [f'import {api};', f'// @{api}()', f'/* @{api}() */',
                       f'import {api}; Owner.Request request;', f'import {api}; String path=Owner.PATH;']:
            self.assertFalse(uses_api(source, api))

    def test_media_annotation_wires_existing_access_permissions_transport(self):
        shell = runtime.SHELL
        self.assertIn('media', usage(shell, runtime.BACKEND)['AccessPermissions'])

if __name__ == '__main__':
    unittest.main()
