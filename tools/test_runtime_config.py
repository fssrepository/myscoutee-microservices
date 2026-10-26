"""Project isolation checks; no host/VM/database mutations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_config as runtime
import workloads


class RuntimeConfigTest(unittest.TestCase):
    # Image inventories are build outputs. Their validation is covered by the
    # seed-volume/lock tests; composition tests must not require a prior build.
    @patch.object(workloads, 'image_lock', return_value={'fixture': {'reference': 'fixture@sha256:example'}})
    def test_explicit_chart_inputs_do_not_contact_runtime_or_open_lab_credentials(self, images):
        credentials = {'delegationClient-' + name: 'analysis-placeholder'
                       for name, service in workloads.SERVICES.items()
                       if 'accounts' not in service['domains']}
        forbidden = AssertionError('Offline chart composition accessed runtime state')
        with patch.object(workloads, 'secrets_data', side_effect=forbidden), \
             patch.object(workloads, 'kube_json', side_effect=forbidden), \
             patch.object(workloads, 'release_simulator_certificate', side_effect=forbidden):
            values = workloads.chart_values(credentials=credentials, cluster_dns='192.0.2.53',
                                             release_certificate='analysis-certificate')
        self.assertIs(values['credentials'], credentials)
        self.assertIs(values['images'], images.return_value)
        self.assertEqual(values['clusterDns'], '192.0.2.53')
        self.assertEqual(values['releaseSimulatorCertificate'], 'analysis-certificate')
        self.assertEqual(set(values['services']), set(workloads.SERVICES))

    @patch.object(workloads, 'image_lock', return_value={'fixture': {'reference': 'fixture@sha256:example'}})
    def test_chart_default_runtime_providers_match_explicit_inputs(self, images):
        credentials = {'delegationClient-' + name: 'analysis-placeholder'
                       for name, service in workloads.SERVICES.items()
                       if 'accounts' not in service['domains']}
        with patch.object(workloads, 'secrets_data', return_value=credentials) as secrets_provider, \
             patch.object(workloads, 'kube_json', return_value={'spec': {'clusterIP': '192.0.2.53'}}) as dns_provider, \
             patch.object(workloads, 'release_simulator_certificate', return_value='analysis-certificate') as certificate_provider:
            actual = workloads.chart_values()
        expected = workloads.chart_values(credentials=credentials, cluster_dns='192.0.2.53',
                                          release_certificate='analysis-certificate')
        self.assertEqual(actual, expected)
        secrets_provider.assert_called_once_with()
        dns_provider.assert_called_once()
        certificate_provider.assert_called_once_with()

    def test_runtime_reports_the_product_release_version(self):
        package = json.loads((runtime.BACKEND / 'frontend/package.json').read_text())
        self.assertEqual(runtime.CONFIG['environment']['MYSCOUTEE_VERSION'], package['version'])

    def test_state_cannot_escape_or_select_an_absolute_host_path(self):
        for value in ('/var/lib/libvirt', '../other', 'lab/../../other', ''):
            with self.subTest(value=value):
                config = copy.deepcopy(runtime.CONFIG)
                config['lab']['stateDirectory'] = value
                with self.assertRaises(ValueError):
                    runtime.validate(config)

    def test_real_demo_and_bootstrap_cannot_share_a_database(self):
        config = copy.deepcopy(runtime.CONFIG)
        config['databases']['demo'] = config['databases']['real']
        with self.assertRaisesRegex(ValueError, 'distinct'):
            runtime.validate(config)

    def test_vm_memory_keeps_the_project_value_without_lab_caps(self):
        for value in (2048, 6144, 8192, 16384):
            config = copy.deepcopy(runtime.CONFIG)
            config['lab']['memoryMiB'] = value
            self.assertEqual(runtime.validate(config)['lab']['memoryMiB'], value)
        for value in (0, -1, True, 1.5, '8192'):
            config['lab']['memoryMiB'] = value
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                runtime.validate(config)

    def test_rollout_timeout_is_bounded_and_allows_first_start_import(self):
        for value in (True, 0, -1, 1801, '600'):
            config = copy.deepcopy(runtime.CONFIG)
            config['rolloutTimeoutSeconds'] = value
            with self.assertRaisesRegex(ValueError, 'rolloutTimeoutSeconds'):
                runtime.validate(config)
        config['rolloutTimeoutSeconds'] = 600
        self.assertEqual(runtime.validate(config)['rolloutTimeoutSeconds'], 600)

    def test_vm_network_rejects_public_loopback_and_link_local_subnets(self):
        for value in ('8.8.8.0/24', '127.0.0.0/24', '169.254.1.0/24', '192.168.173.0/25'):
            with self.subTest(value=value):
                config = copy.deepcopy(runtime.CONFIG)
                config['lab']['networkCidr'] = value
                with self.assertRaises(ValueError):
                    runtime.validate(config)

    def test_project_ports_do_not_alias_monitoring_and_application(self):
        config = copy.deepcopy(runtime.CONFIG)
        config['lab']['grafanaPort'] = config['localPort']
        with self.assertRaisesRegex(ValueError, 'distinct'):
            runtime.validate(config)

    def test_omitted_dependencies_do_not_create_product_credentials_or_objects(self):
        config = copy.deepcopy(workloads.CONFIG)
        for dependency in ('registry', 'payment-simulator'):
            config['dependencies'].pop(dependency, None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(workloads, 'CONFIG', config), \
             patch.object(workloads, 'TRANSPORTS', [binding for binding in workloads.TRANSPORTS
                                                   if binding['domain'] not in ('payments', 'registry')]), \
             patch.object(workloads, 'SECRET_FILE', Path(directory) / 'secrets.json'):
            first = workloads.secrets_data()
            second = workloads.secrets_data()
            self.assertEqual(first, second)
            self.assertFalse(any(key.startswith('paymentSimulator') for key in first))
            self.assertFalse(any(name in ('registry', 'registry-client', 'payment-simulator')
                                 for _, name in workloads.chart_objects()))
            self.assertIn(('Secret', 'deployment-configuration'), workloads.chart_objects())
            self.assertIn(('Service', 'mcp'), workloads.chart_objects())


if __name__ == '__main__':
    unittest.main()
