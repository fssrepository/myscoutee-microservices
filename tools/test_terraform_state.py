import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import terraform_state


class BackendTests(unittest.TestCase):
    def test_local_default_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            path=Path(directory)/'state'; path.write_text('{"serial":7}')
            self.assertEqual(terraform_state.read_state(path),{'serial':7})

    def test_http_state_is_authoritative_and_never_masks_access_failure(self):
        env={'MYSCOUTEE_TERRAFORM_BACKEND':'gitlab','TF_HTTP_ADDRESS':'http://localhost/state',
             'TF_HTTP_USERNAME':'gitlab-ci-token','TF_HTTP_PASSWORD':'private-test-value'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,env,clear=True):
            path=Path(directory)/'state'
            response=io.BytesIO(b'{"serial":9}'); response.status=200
            with patch('terraform_state.urllib.request.urlopen',return_value=response):
                self.assertEqual(terraform_state.read_state(path),{'serial':9})
            for code in (403,500):
                error=urllib.error.HTTPError('http://localhost/state',code,'failure',{},None)
                with patch('terraform_state.urllib.request.urlopen',side_effect=error),self.assertRaises(RuntimeError):
                    terraform_state.read_state(path)
            path.write_text('{"serial":7}')
            with self.assertRaisesRegex(RuntimeError,'migrate'):
                terraform_state.read_state(path)

    def test_missing_remote_state_is_empty_but_requires_credentials(self):
        env={'MYSCOUTEE_TERRAFORM_BACKEND':'gitlab','TF_HTTP_ADDRESS':'http://localhost/state',
             'TF_HTTP_USERNAME':'gitlab-ci-token','TF_HTTP_PASSWORD':'private-test-value'}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,env,clear=True):
            error=urllib.error.HTTPError('http://localhost/state',404,'missing',{},None)
            with patch('terraform_state.urllib.request.urlopen',side_effect=error):
                self.assertEqual(terraform_state.read_state(Path(directory)/'state'),{})
            del os.environ['TF_HTTP_PASSWORD']
            with self.assertRaises(ValueError): terraform_state.remote()

    def test_newly_locked_state_has_no_version_yet(self):
        env={'MYSCOUTEE_TERRAFORM_BACKEND':'gitlab','TF_HTTP_ADDRESS':'http://localhost/state',
             'TF_HTTP_USERNAME':'gitlab-ci-token','TF_HTTP_PASSWORD':'private-test-value'}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,env,clear=True):
            response=io.BytesIO(b''); response.status=204
            with patch('terraform_state.urllib.request.urlopen',return_value=response):
                self.assertEqual(terraform_state.read_state(Path(directory)/'state'),{})
            response=io.BytesIO(b''); response.status=200
            with patch('terraform_state.urllib.request.urlopen',return_value=response), self.assertRaises(json.JSONDecodeError):
                terraform_state.read_state(Path(directory)/'state')
