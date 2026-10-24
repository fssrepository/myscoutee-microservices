"""Safety gates with disposable state; these tests do not claim a provisioned VM."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time
import unittest
from unittest.mock import patch


def module(name):
    source = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location(name, source)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


host = module('host')
vm = module('vm')
workloads = module('workloads')
seed_mongo = __import__('seed_mongo')
mongo_replica = module('mongo_replica')


class PerformanceEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.measure = module('measure-stress')
        self.before = {'containers': [{'id': 'first', 'pod': 'identity-pod', 'container': 'identity',
            'at': 10, 'cpu': {'usage_usec': 1_000_000, 'throttled_usec': 100_000},
            'cpuMax': '200000 100000', 'memoryBytes': 100 * 1024 ** 2}],
            'host': {'at': 10, 'cpuTotal': 1000, 'cpuIdle': 200, 'logicalCpus': 8, 'qemuCpuNs': 2_000_000_000}}
        self.after = json.loads(json.dumps(self.before))
        self.after['containers'][0].update(at=20, cpu={'usage_usec': 16_000_000, 'throttled_usec': 800_000})
        self.after['host'].update(at=20, cpuTotal=2000, cpuIdle=600, qemuCpuNs=12_000_000_000)

    def test_cpu_units_distinguish_one_core_from_host_percentage(self):
        row, = self.measure.resource_deltas(self.before, self.after)
        self.assertEqual(1.5, row['cpuCores'])
        self.assertEqual(.7, row['throttledSeconds'])
        host = self.measure.host_delta(self.before, self.after)
        self.assertEqual(60, host['busyPercent'])
        self.assertEqual(1, host['qemuCpuCores'])

    def test_container_replacement_or_removal_invalidates_a_comparable_sample(self):
        self.after['containers'][0]['id'] = 'replacement'
        self.assertTrue(all(row['replacedDuringRun'] for row in self.measure.resource_deltas(self.before, self.after)))
        self.after['containers'] = []
        row, = self.measure.resource_deltas(self.before, self.after)
        self.assertTrue(row['replacedDuringRun'])

    def test_counter_reset_is_not_reported_as_negative_cpu(self):
        self.after['containers'][0]['cpu']['usage_usec'] = 0
        with self.assertRaisesRegex(ValueError, 'counter interval'):
            self.measure.resource_deltas(self.before, self.after)


class MongoStatefulMigrationTest(unittest.TestCase):
    def row(self):
        return {'target': 'data-mongodb-0', 'claim': {'metadata': {'name': 'mongodb', 'uid': 'claim-uid'}},
                'pv': {'metadata': {'uid': 'volume-uid'}, 'spec': {'local': {'path': '/original'},
                    'nodeAffinity': {'node': 'owned'}, 'claimRef': {'name': 'mongodb',
                        'namespace': workloads.NAMESPACE, 'uid': 'claim-uid'}}}}

    def test_rebind_rejects_changed_disk_location_and_foreign_claim(self):
        for mutate in (lambda p: p['metadata'].update(uid='other'),
                       lambda p: p['spec']['local'].update(path='/empty'),
                       lambda p: p['spec']['claimRef'].update(uid='foreign')):
            row = self.row()
            pv = json.loads(json.dumps(row['pv']))
            mutate(pv)
            with self.assertRaises(RuntimeError):
                mongo_replica.validate_volume(row, pv)

    def test_rebind_accepts_only_original_or_recorded_target_claim(self):
        row = self.row()
        mongo_replica.validate_volume(row, row['pv'])
        row['targetUid'] = 'new-uid'
        pv = json.loads(json.dumps(row['pv']))
        pv['spec']['claimRef'].update(name='data-mongodb-0', uid='new-uid')
        mongo_replica.validate_volume(row, pv)

    def test_rebind_retains_volume_before_releasing_claim_and_pins_target(self):
        row = self.row()
        row['pv']['metadata'].update(name='pv-original', resourceVersion='1')
        row['pv']['spec']['persistentVolumeReclaimPolicy'] = 'Delete'
        row['claim']['metadata']['labels'] = workloads.LABEL
        row['claim']['spec'] = {'volumeName': 'pv-original', 'storageClassName': 'local-path',
                                'accessModes': ['ReadWriteOnce'], 'volumeMode': 'Filesystem',
                                'resources': {'requests': {'storage': '4Gi'}}}
        pv = json.loads(json.dumps(row['pv']))
        claims = {'mongodb': row['claim']}
        operations = []
        def command(*args, **kwargs):
            if args[:2] == ('patch', 'pv'):
                patch_ops = json.loads(args[-1])
                change = patch_ops[-1]
                if change['path'].endswith('persistentVolumeReclaimPolicy'):
                    pv['spec']['persistentVolumeReclaimPolicy'] = change['value']
                    operations.append('retain')
                else:
                    self.assertNotIn('mongodb', claims)
                    self.assertEqual('new-uid', change['value']['uid'])
                    pv['spec']['claimRef'] = change['value']
                    operations.append('rebind')
            elif args[0] == 'create':
                target = json.loads(kwargs['data'])
                self.assertEqual('pv-original', target['spec']['volumeName'])
                target['metadata']['uid'] = 'new-uid'
                claims[target['metadata']['name']] = target
                operations.append('prebind')
            elif 'delete' in args:
                self.assertEqual('Retain', pv['spec']['persistentVolumeReclaimPolicy'])
                del claims['mongodb']
                operations.append('release')
        with patch.object(mongo_replica.w, 'kube_json', side_effect=lambda *a: json.loads(json.dumps(pv))), \
             patch.object(mongo_replica.w, 'kube', side_effect=command), \
             patch.object(mongo_replica, 'existing', side_effect=lambda kind, name: claims.get(name)), \
             patch.object(mongo_replica, 'save_state'):
            mongo_replica.rebind_volume(row, {'volumes': [row]})
        self.assertEqual(['retain', 'prebind', 'release', 'rebind'], operations)

    def test_chart_cannot_implicitly_replace_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(mongo_replica, 'STATEFUL_STATE', Path(directory) / 'receipt'), \
             patch.object(mongo_replica, 'existing', return_value={'kind': 'Deployment'}):
            with self.assertRaisesRegex(RuntimeError, 'mongo_replica.py statefulset'):
                mongo_replica.require_stateful_volumes()

    def test_partial_migration_blocks_normal_chart_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt'
            receipt.write_text(json.dumps({'phase': 'backed-up'}))
            with patch.object(mongo_replica, 'STATEFUL_STATE', receipt), \
                 patch.object(mongo_replica, 'existing', return_value=None), \
                 patch.object(mongo_replica, 'verify_receipt'):
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    mongo_replica.require_stateful_volumes()

    def test_stateful_member_exec_keeps_ordinal_identity(self):
        with patch.object(mongo_replica, 'workload', return_value='statefulset/mongodb'):
            self.assertEqual('pod/mongodb-0', mongo_replica.member_target())
            self.assertEqual('pod/mongodb-1', mongo_replica.member_target('mongodb-secondary'))


class MongoServiceMigrationTest(unittest.TestCase):
    def service(self, name='mongodb'):
        return {'metadata': {'name': name, 'uid': 'owned-uid', 'labels': workloads.LABEL}, 'spec': {'clusterIP': '10.43.0.5'}}

    def test_member_dns_migration_refuses_active_backends(self):
        with patch.object(workloads, 'backend_deployments', return_value=[{'spec': {'replicas': 1}}]), patch.object(workloads, 'kube') as command:
            with self.assertRaisesRegex(RuntimeError, 'backends-stop'):
                workloads.migrate_mongo_member_services([self.service()])
            command.assert_not_called()

    def test_member_dns_migration_rejects_foreign_or_replaced_services(self):
        with patch.object(workloads, 'backend_deployments', return_value=[]), patch.object(workloads, 'kube') as command:
            with self.assertRaisesRegex(RuntimeError, 'unowned'):
                workloads.migrate_mongo_member_services([self.service('other')])
            current = self.service()
            current['metadata']['uid'] = 'replacement'
            with patch.object(workloads, 'kube_json', return_value=current):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    workloads.migrate_mongo_member_services([self.service()])
            command.assert_not_called()

    def test_member_dns_migration_only_replaces_the_exact_service(self):
        service = self.service()
        with patch.object(workloads, 'backend_deployments', return_value=[]), patch.object(workloads, 'kube_json', return_value=service), patch.object(workloads, 'kube') as command:
            workloads.migrate_mongo_member_services([service])
            command.assert_called_once_with('-n', workloads.NAMESPACE, 'delete', 'service', 'mongodb', '--wait=true')


class MongoConversionSafetyTest(unittest.TestCase):
    def test_cleanup_checks_the_exact_backup_hash_and_preserves_extra_files(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'backups'
            folder.mkdir()
            archive = folder / 'mongo-before-replicaset-20261009T010000Z.archive.gz'
            archive.write_bytes(b'owned')
            (root / 'mongo-replicaset.json').write_text(json.dumps({
                'backup': str(archive.relative_to(root)), 'sha256': hashlib.sha256(b'owned').hexdigest()}))
            with patch.object(vm, 'VM', root):
                self.assertEqual(vm.mongo_backup_cleanup(), [archive])
                archive.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    vm.mongo_backup_cleanup()
                archive.write_bytes(b'owned')
                (folder / 'user-backup.gz').write_bytes(b'keep')
                with self.assertRaisesRegex(RuntimeError, 'Unexpected Mongo backup entries'):
                    vm.mongo_backup_cleanup()
                self.assertTrue(archive.exists())

    def test_existing_volume_without_backup_receipt_is_refused(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(mongo_replica, 'STATE', Path(directory) / 'state.json'), \
             patch.object(mongo_replica.w, 'kube_json', return_value={'metadata': {'uid': 'owned'}}), \
             patch.object(mongo_replica.w, 'kube', return_value=SimpleNamespace(stdout='{"kind":"PersistentVolumeClaim"}')):
            with self.assertRaisesRegex(RuntimeError, 'Existing Mongo data'):
                mongo_replica.require_conversion_receipt()

    def test_foreign_namespace_receipt_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({'namespaceUid': 'other'}))
            with patch.object(mongo_replica, 'STATE', state), \
                 patch.object(mongo_replica.w, 'kube_json', return_value={'metadata': {'uid': 'owned'}}):
                with self.assertRaisesRegex(RuntimeError, 'another namespace'):
                    mongo_replica.require_conversion_receipt()

    def test_changed_backup_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text(json.dumps({'namespaceUid': 'owned', 'backup': 'dump.gz', 'sha256': 'bad'}))
            (root / 'dump.gz').write_bytes(b'changed')
            with patch.object(mongo_replica, 'STATE', state), patch.object(mongo_replica.w.vm, 'VM', root), \
                 patch.object(mongo_replica.w, 'kube_json', return_value={'metadata': {'uid': 'owned'}}):
                with self.assertRaisesRegex(RuntimeError, 'missing or changed'):
                    mongo_replica.require_conversion_receipt()

    def test_foreign_replica_members_are_never_reconfigured(self):
        from types import SimpleNamespace
        with patch.object(mongo_replica.w, 'kube', return_value=SimpleNamespace(stdout='{"status":{"readyReplicas":1}}')), \
             patch.object(mongo_replica, 'direct', return_value={'config': {'_id': 'msc121', 'members': [{'host': 'foreign:27017'}]}}):
            with self.assertRaisesRegex(RuntimeError, 'refusing reconfiguration'):
                mongo_replica.configured()


class ReleaseSimulatorSafetyTest(unittest.TestCase):
    def test_only_one_public_certificate_can_enter_backend_config(self):
        from types import SimpleNamespace
        for value in ('-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----',
                      '-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n'
                      '-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----', 'x' * 8193):
            with patch.object(workloads, 'kube', return_value=SimpleNamespace(stdout=value)):
                with self.assertRaisesRegex(RuntimeError, 'public TLS certificate'):
                    workloads.release_simulator_certificate()
        value = '-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n'
        with patch.object(workloads, 'kube', return_value=SimpleNamespace(stdout=value)):
            self.assertEqual(value, workloads.release_simulator_certificate())

    def test_ready_foreign_deployment_cannot_supply_lab_trust(self):
        from types import SimpleNamespace
        deployment = {'metadata': {'labels': {}}, 'status': {'availableReplicas': 1}}
        with patch.object(workloads, 'kube', return_value=SimpleNamespace(stdout=json.dumps(deployment))):
            with self.assertRaisesRegex(RuntimeError, 'Unowned'):
                workloads.release_simulator_ready()


class BackendUpgradeSafetyTest(unittest.TestCase):
    def deployment(self, image, replicas):
        return {'metadata': {'name': 'identity'}, 'spec': {'replicas': replicas,
                'template': {'spec': {'containers': [{'image': image}]}}}, 'status': {'replicas': replicas}}

    def test_new_image_cannot_mix_with_running_old_writers(self):
        with patch.object(workloads, 'backend_deployments', return_value=[self.deployment('old', 1)]), \
             patch.object(workloads, 'kube_json') as kube:
            with self.assertRaisesRegex(RuntimeError, 'backends-stop'):
                workloads.require_backend_upgrade_quiescence({'identity': {'reference': 'new'}})
            kube.assert_not_called()

    def test_identical_image_start_does_not_require_shutdown(self):
        with patch.object(workloads, 'backend_deployments', return_value=[self.deployment('same', 1)]), \
             patch.object(workloads, 'kube_json') as kube:
            workloads.require_backend_upgrade_quiescence({'identity': {'reference': 'same'}})
            kube.assert_not_called()

    def test_terminating_old_pod_prevents_upgrade_even_at_zero_replicas(self):
        with patch.object(workloads, 'backend_deployments', return_value=[self.deployment('old', 0)]), \
             patch.object(workloads, 'kube_json', return_value={'items': [
                 {'metadata': {'labels': {'app.kubernetes.io/name': 'identity'}, 'deletionTimestamp': 'now'}}]}):
            with self.assertRaisesRegex(RuntimeError, 'terminate'):
                workloads.require_backend_upgrade_quiescence({'identity': {'reference': 'new'}})

    def test_quiesced_backends_leave_support_pods_running(self):
        with patch.object(workloads, 'backend_deployments', return_value=[self.deployment('old', 0)]), \
             patch.object(workloads, 'kube_json', return_value={'items': [
                 {'metadata': {'labels': {'app.kubernetes.io/name': 'mongodb'}}}]}):
            workloads.require_backend_upgrade_quiescence({'identity': {'reference': 'new'}})


class SeedBootstrapSafetyTest(unittest.TestCase):


    def catalog_files(self, directory):
        path = Path(directory) / 'landingSlides.json'
        path.write_text('[{"_id":"slide-1","title":"Canonical slide"}]')
        importer = Path(directory) / 'mongo_import.sh'
        importer.write_text('# shared import engine\n')
        policy = Path(directory) / 'mongo-seed-policy.inc'
        policy.write_text('# project policy\n')
        return {'mongo_import.sh': importer, 'mongo-seed-policy.inc': policy,
                'myscoutee_db/landingSlides.json': path, 'demo_db/landingSlides.json': path}

    def test_catalog_migration_imports_only_missing_catalog_and_database(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo, 'catalog_baseline_fingerprint', return_value='old'), \
             patch.object(seed_mongo, 'mongo', side_effect=[
                 {'fingerprint': 'old', 'missing': [seed_mongo.REAL_DB]},
                 {'fingerprint': 'old', 'missing': []}, {'updated': True}]), \
             patch.object(seed_mongo, 'import_files') as importer:
            files = self.catalog_files(directory)
            seed_mongo.add_catalog(files, f'{seed_mongo.REAL_DB}:landingSlides;{seed_mongo.DEMO_DB}:*',
                                   'new', 'owned', 'landingSlides', 'old')
            importer.assert_called_once_with(
                {'mongo_import.sh': files['mongo_import.sh'],
                 'mongo-seed-policy.inc': files['mongo-seed-policy.inc'],
                 'myscoutee_db/landingSlides.json': files['myscoutee_db/landingSlides.json']},
                f'{seed_mongo.REAL_DB}:landingSlides', 'new', {seed_mongo.REAL_DB: 'myscoutee_db'}, catalog='landingSlides')

    def test_completed_catalog_migration_is_a_noop_and_never_repairs_deleted_data(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo, 'catalog_baseline_fingerprint', return_value='old'), \
             patch.object(seed_mongo, 'import_files') as importer:
            files = self.catalog_files(directory)
            with patch.object(seed_mongo, 'mongo', return_value={'fingerprint': 'new', 'missing': []}) as mongo:
                seed_mongo.add_catalog(files, f'{seed_mongo.REAL_DB}:landingSlides;{seed_mongo.DEMO_DB}:*',
                                       'new', 'owned', 'landingSlides', 'old')
                self.assertEqual(mongo.call_count, 2)
            with patch.object(seed_mongo, 'mongo', return_value={'fingerprint': 'new', 'missing': [seed_mongo.REAL_DB]}):
                with self.assertRaisesRegex(RuntimeError, 'refusing automatic repair'):
                    seed_mongo.add_catalog(files, f'{seed_mongo.REAL_DB}:landingSlides;{seed_mongo.DEMO_DB}:*',
                                           'new', 'owned', 'landingSlides', 'old')
            importer.assert_not_called()

    def test_catalog_guard_failure_cannot_import_or_update_bootstrap_marker(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo, 'catalog_baseline_fingerprint', return_value='old'), \
             patch.object(seed_mongo, 'mongo', side_effect=RuntimeError('Existing catalog differs')) as mongo, \
             patch.object(seed_mongo, 'import_files') as importer:
            with self.assertRaisesRegex(RuntimeError, 'Existing catalog differs'):
                seed_mongo.add_catalog(self.catalog_files(directory), f'{seed_mongo.REAL_DB}:landingSlides;{seed_mongo.DEMO_DB}:*',
                                       'new', 'owned', 'landingSlides', 'old')
            mongo.assert_called_once()
            importer.assert_not_called()

    def test_catalog_migration_rejects_unrelated_seed_source_changes_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo, 'mongo') as mongo, \
             patch.object(seed_mongo, 'import_files') as importer:
            files = self.catalog_files(directory)
            files['mongo-seed-policy.inc'].write_text(
                'allowed_collections_by_db="myscoutee_db:appSettings,landingSlides,other;production_db:appSettings,landingSlides,other"\n')
            allowlist = 'msc121_real:appSettings,landingSlides,other;msc121_demo:*'
            before = seed_mongo.catalog_baseline_fingerprint(files, allowlist, 'landingSlides')
            files['demo_db/landingSlides.json'].write_text('[{"_id":"changed"}]')
            with self.assertRaisesRegex(RuntimeError, 'Source changes exceed'):
                seed_mongo.add_catalog(files, allowlist, 'new', 'owned', 'landingSlides', before)
            mongo.assert_not_called()
            importer.assert_not_called()

    def test_engine_and_policy_changes_invalidate_catalog_migration(self):
        for filename in seed_mongo.IMPORT_FILES:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory, \
                 patch.object(seed_mongo, 'mongo') as mongo:
                files = self.catalog_files(directory)
                files['mongo-seed-policy.inc'].write_text(
                    'allowed_collections_by_db="myscoutee_db:appSettings,landingSlides,other;production_db:appSettings,landingSlides,other"\n')
                allowlist = 'msc121_real:appSettings,landingSlides,other;msc121_demo:*'
                before = seed_mongo.catalog_baseline_fingerprint(files, allowlist, 'landingSlides')
                files[filename].write_text(files[filename].read_text() + '# changed\n')
                with self.assertRaisesRegex(RuntimeError, 'Source changes exceed'):
                    seed_mongo.add_catalog(files, allowlist, 'new', 'owned', 'landingSlides', before)
                mongo.assert_not_called()

    def test_live_backends_prevent_any_seed_mutation(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(seed_mongo.w.vm, 'VM', Path(directory)), \
             patch.object(seed_mongo, 'seed_bundle', return_value=({}, '', 'fixture')), \
             patch.object(seed_mongo.w, 'namespace_owned', return_value={'metadata': {'uid': 'owned'}}), \
             patch.object(seed_mongo.w, 'kube_json', return_value={'spec': {'replicas': 1}}), \
             patch.object(seed_mongo, 'mongo') as mongo:
            with self.assertRaisesRegex(RuntimeError, 'Stop all owned backends'):
                seed_mongo.main([])
            mongo.assert_not_called()

    def test_restart_detects_changed_content_without_reimporting(self):
        with patch.object(seed_mongo, 'snapshot', side_effect=[{'seed':'before'},{'seed':'after'}]), \
             patch.object(seed_mongo.w, 'kube') as kube, \
             patch.object(seed_mongo, 'claim_bootstrap') as claim:
            with self.assertRaisesRegex(RuntimeError, 'changed across pod replacement'):
                seed_mongo.verify_persistence('fingerprint','owned',True)
            self.assertEqual(3,kube.call_count)
            claim.assert_not_called()

    def test_real_database_cannot_inherit_a_wildcard_demo_seed_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory)
            (backend / 'server').mkdir()
            (backend / 'server/.env').write_text('MONGO_IMPORT_ALLOWED_COLLECTIONS_BY_DB=myscoutee_db:*;demo_db:*\n')
            with self.assertRaisesRegex(RuntimeError, 'seed policy'):
                seed_mongo.seed_bundle(backend)

    def test_seed_links_cannot_archive_files_outside_the_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory) / 'backend'
            source = backend / 'server/docker/conf/mongodb'
            (source / 'myscoutee_db').mkdir(parents=True)
            (backend / 'server/.env').write_text('MONGO_IMPORT_ALLOWED_COLLECTIONS_BY_DB=myscoutee_db:appSettings;demo_db:*\n')
            foreign = Path(directory) / 'private.json'
            foreign.write_text('[]')
            (source / 'myscoutee_db/appSettings.json').symlink_to(foreign)
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                seed_mongo.seed_bundle(backend)


class WorkloadCredentialsTest(unittest.TestCase):


    def test_constants_and_nested_dtos_do_not_grant_a_remote_capability(self):
        from transport_usage import uses_api
        api = 'com.raxim.myscoutee.domain.feedback.api.FeedbackPreparation'
        self.assertFalse(uses_api('import ' + api + '; class Reader { Object value = FeedbackPreparation.CRITERIA; }', api))
        self.assertFalse(uses_api('import ' + api + '; class Reader { FeedbackPreparation.Request request; }', api))
        self.assertTrue(uses_api('import ' + api + '; class Reader { private FeedbackPreparation owner; }', api))
        self.assertTrue(uses_api('class Reader { Reader(' + api + ' owner) {} }', api))
        self.assertTrue(uses_api('import ' + api + '; class Reader { private ObjectProvider<FeedbackPreparation> owner; }', api))
        self.assertTrue(uses_api('@Import(' + api + '.class) class Config {}', api))

    def test_shared_parameter_reader_gets_only_its_catalog_capability(self):
        binding = {'domain': 'affinity', 'api': 'ConfigurationCatalog',
                   'consumerApis': ['com.raxim.myscoutee.domain.platform.api.NotificationPolicy'],
                   'propertyPrefix': 'configuration-catalog', 'credential': 'configurationCatalogToken'}
        services = {'affinity': {'domains': ['affinity']}, 'communication': {'domains': ['notifications']},
                    'assets': {'domains': ['assets']}}
        environment = 'MYSCOUTEE_REMOTE_CONFIGURATION_CATALOG_READ_TOKEN'
        with tempfile.TemporaryDirectory() as directory, patch.object(workloads, 'TRANSPORTS', [binding]):
            source = Path(directory) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain/notifications/service/Worker.java'
            source.parent.mkdir(parents=True)
            source.write_text('import com.raxim.myscoutee.domain.platform.api.NotificationPolicy; class Consumer { private NotificationPolicy owner; }')
            scoped = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({environment: 'configurationCatalogToken'}, scoped['affinity'])
            self.assertEqual({environment: 'configurationCatalogToken'}, scoped['communication'])
            self.assertEqual({}, scoped['assets'])

    def test_actor_forwarding_uses_actual_capabilities_and_follows_domain_regrouping(self):
        operation = {'audience': 'accounts:selected-profile', 'method': 'POST',
                     'path': '/_internal/v1/accounts/selected-profile'}
        binding = {'domain': 'accounts', 'credential': 'selectedProfileToken', 'actorOperations': [operation]}
        services = {'identity': {'domains': ['accounts']}, 'messages': {'domains': ['chat']},
                    'unrelated': {'domains': ['assets']}}
        credentials = {'delegationClient-messages': 'a' * 43, 'delegationClient-unrelated': 'b' * 43}
        capabilities = {'identity': {}, 'messages': {'SELECTED_PROFILE': 'selectedProfileToken'}, 'unrelated': {}}
        with patch.object(workloads, 'TRANSPORTS', [binding]):
            settings = workloads.actor_delegation_settings(services, credentials, capabilities)
            self.assertEqual('identity', settings['owner'])
            self.assertNotIn('identity', settings['clients'])
            self.assertEqual([operation], settings['clients']['messages']['operations'])
            self.assertEqual([], settings['clients']['unrelated']['operations'])
            self.assertEqual(['assembly:public:messages'], settings['clients']['messages']['audiences'])
            services['new-owner'] = services.pop('identity')
            capabilities['new-owner'] = capabilities.pop('identity')
            moved = workloads.actor_delegation_settings(services, credentials, capabilities)
            self.assertEqual('new-owner', moved['owner'])
            self.assertEqual(settings['clients'], moved['clients'])

    def test_legacy_secret_cleanup_removes_only_revoked_transport_keys(self):
        retained = 'MYSCOUTEE_REMOTE_OWNED_READ_TOKEN'
        revoked = 'MYSCOUTEE_REMOTE_FOREIGN_READ_TOKEN'
        secret = {'metadata': {'resourceVersion': '42'},
                  'data': {retained: 'kept', revoked: 'removed', 'MONGODB_DEMO_URI': 'untouched'}}
        migration = workloads.stale_transport_key_patch(secret, {retained})
        self.assertEqual([
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'},
            {'op': 'remove', 'path': '/data/' + revoked}], migration)
        del secret['data'][revoked]
        self.assertEqual([], workloads.stale_transport_key_patch(secret, {retained}))
        self.assertEqual('/data/' + retained, workloads.stale_transport_key_patch(secret, {})[1]['path'])

    def test_inherited_api_consumer_uses_the_same_owner_capability(self):
        binding = {'domain': 'access', 'api': 'AccessPermissions', 'providedApis': ['ConsentAuthorization'],
                   'propertyPrefix': 'access-permissions', 'credential': 'accessPermissionsToken'}
        services = {'identity': {'domains': ['access']}, 'community': {'domains': ['help']},
                    'unrelated': {'domains': ['email']}}
        environment = 'MYSCOUTEE_REMOTE_ACCESS_PERMISSIONS_READ_TOKEN'
        with tempfile.TemporaryDirectory() as directory, patch.object(workloads, 'TRANSPORTS', [binding]):
            source = Path(directory) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain/help/service/Help.java'
            source.parent.mkdir(parents=True)
            source.write_text('import com.raxim.myscoutee.domain.access.api.ConsentAuthorization; class Consumer { private ConsentAuthorization owner; }')
            scoped = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({environment: 'accessPermissionsToken'}, scoped['identity'])
            self.assertEqual({environment: 'accessPermissionsToken'}, scoped['community'])
            self.assertEqual({}, scoped['unrelated'])

    def test_extracted_consumer_keeps_exact_capability_without_application_source(self):
        binding = {'domain': 'accounts', 'api': 'AccountMediaSessions',
                   'configuration': 'AccountMediaSessions', 'consumerDomains': ['media'],
                   'propertyPrefix': 'account-media-sessions', 'credential': 'mediaToken',
                   'producerDomains': ['media']}
        services = {'identity': {'domains': ['accounts']}, 'assets': {'domains': ['media']},
                    'unrelated': {'domains': ['email']}}
        environment = 'MYSCOUTEE_REMOTE_ACCOUNT_MEDIA_SESSIONS_READ_TOKEN'
        with tempfile.TemporaryDirectory() as directory, patch.object(workloads, 'TRANSPORTS', [binding]):
            result = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({environment: 'mediaToken'}, result['identity'])
            self.assertEqual({environment: 'mediaToken'}, result['assets'])
            self.assertEqual({}, result['unrelated'])
            binding['consumerDomains'] = ['email']
            with self.assertRaisesRegex(RuntimeError, 'Unauthorized producer domain'):
                workloads.transport_credentials_by_service(services, directory)
            binding['consumerDomains'] = ['unknown']
            with self.assertRaisesRegex(ValueError, 'Invalid consumer domains'):
                workloads.transport_credentials_by_service(services, directory)

    def test_unrelated_domain_cannot_become_an_account_change_producer(self):
        binding = {'domain': 'affinity', 'api': 'UserAffinityChanges', 'propertyPrefix': 'user-affinity-changes',
                   'credential': 'userAffinityChangesToken', 'credentialProperty': 'command-token',
                   'producerDomains': ['accounts']}
        services = {'identity': {'domains': ['accounts']}, 'affinity': {'domains': ['affinity']},
                    'unrelated': {'domains': ['email']}}
        with tempfile.TemporaryDirectory() as directory, patch.object(workloads, 'TRANSPORTS', [binding]):
            root = Path(directory) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain'
            for domain in ['accounts', 'email']:
                source = root / domain / 'service/Consumer.java'
                source.parent.mkdir(parents=True)
                source.write_text('import com.raxim.myscoutee.domain.affinity.api.UserAffinityChanges; class Consumer { private UserAffinityChanges owner; }')
            with self.assertRaisesRegex(RuntimeError, 'Unauthorized producer domain'):
                workloads.transport_credentials_by_service(services, directory)

    def test_transport_credentials_follow_owners_and_actual_consumers(self):
        bindings = [{'domain': 'affinity', 'api': 'UserAffinityScores', 'propertyPrefix': 'user-affinity',
                     'credential': 'userAffinityToken'}]
        services = {'identity': {'domains': ['accounts']}, 'affinity': {'domains': ['affinity']},
                    'unrelated': {'domains': ['email']}}
        environment = 'MYSCOUTEE_REMOTE_USER_AFFINITY_READ_TOKEN'
        with tempfile.TemporaryDirectory() as directory, patch.object(workloads, 'TRANSPORTS', bindings):
            source = Path(directory) / 'server/projects/profile/src/main/java/com/raxim/myscoutee/domain/accounts/service/Profile.java'
            source.parent.mkdir(parents=True)
            source.write_text('import com.raxim.myscoutee.domain.affinity.api.UserAffinityScores; class Consumer { private UserAffinityScores owner; }')
            scoped = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({environment: 'userAffinityToken'}, scoped['identity'])
            self.assertEqual({environment: 'userAffinityToken'}, scoped['affinity'])
            self.assertEqual({}, scoped['unrelated'])
            source.write_text('class Profile {}')
            scoped = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({}, scoped['identity'])
            self.assertEqual({environment: 'userAffinityToken'}, scoped['affinity'])
            services['affinity']['domains'].remove('affinity')
            services['new-owner'] = {'domains': ['affinity']}
            scoped = workloads.transport_credentials_by_service(services, directory)
            self.assertEqual({}, scoped['affinity'])
            self.assertEqual({environment: 'userAffinityToken'}, scoped['new-owner'])


    def test_interrupted_transport_migration_keeps_original_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / 'workload-secrets.json'
            original = '{"mongoPassword":"fixture-mongo","localizationToken":"fixture-localization"}\n'
            secret_file.write_text(original)
            with patch.object(workloads, 'SECRET_FILE', secret_file), \
                 patch.object(workloads.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    workloads.secrets_data()
            self.assertEqual(original, secret_file.read_text())
            self.assertEqual([secret_file], list(Path(directory).iterdir()))

    def test_incomplete_delegation_keys_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file=Path(directory)/'workload-secrets.json'
            original='{"delegationPublicKey":"existing-public-key"}\n'
            secret_file.write_text(original)
            with patch.object(workloads,'SECRET_FILE',secret_file):
                with self.assertRaisesRegex(RuntimeError,'Incomplete actor delegation'):
                    workloads.secrets_data()
            self.assertEqual(original,secret_file.read_text())

    def test_invalid_delegation_pair_is_not_silently_rotated(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file=Path(directory)/'workload-secrets.json'
            original='{"delegationPublicKey":"invalid","delegationPrivateKey":"invalid"}\n'
            secret_file.write_text(original)
            with patch.object(workloads,'SECRET_FILE',secret_file):
                with self.assertRaisesRegex(RuntimeError,'Invalid actor delegation'):
                    workloads.secrets_data()
            self.assertEqual(original,secret_file.read_text())

    def test_invalid_configuration_key_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / 'workload-secrets.json'
            original = '{"configurationMasterKey":"invalid"}\n'
            secret_file.write_text(original)
            with patch.object(workloads, 'SECRET_FILE', secret_file):
                with self.assertRaisesRegex(RuntimeError, 'Invalid configuration master key'):
                    workloads.secrets_data()
            self.assertEqual(original, secret_file.read_text())



class HostSafetyTest(unittest.TestCase):
    def test_purge_refuses_removing_a_later_dependent_package(self):
        with self.assertRaisesRegex(RuntimeError, 'unowned'):
            host.check_purge('Purg libvirt-clients [8.0]\nRemv another-project [1.0]\n', {'libvirt-clients'})

    def test_purge_accepts_only_the_recorded_package_set(self):
        host.check_purge('Purg libvirt-clients [8.0]\n', {'libvirt-clients'})

    def test_purge_cannot_install_replacements(self):
        with self.assertRaises(RuntimeError):
            host.check_purge('Purg libvirt-clients [8.0]\nInst replacement (2.0)\n', {'libvirt-clients'})

    def test_stop_never_stops_a_running_guest_or_unowned_unit(self):
        state = {'owned_units': ['libvirtd.service'], 'default_network_preexisting': False}
        with patch.object(host, 'unit_value', return_value='active'), \
             patch.object(host, 'no_foreign_resources'), \
             patch.object(host, 'run', return_value=subprocess.CompletedProcess([], 0, 'myscoutee-msc121\n')), \
             patch.object(host, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'Stop the lab VM'):
                host.stop(state)
            execute.assert_not_called()


class VmSafetyTest(unittest.TestCase):
    def test_lifecycle_pause_discards_the_guest_producer_instead_of_queueing_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (state / 'lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(vm, 'VM', state), \
                     patch.object(vm._guest_observer, 'close') as close, \
                     patch.object(vm._guest_observer, 'read') as read, \
                     patch.object(vm, 'monitoring_snapshot', return_value=[]):
                    self.assertIn('lifecycle operation in progress', vm.observe_snapshot())
                    close.assert_called_once()
                    read.assert_not_called()

    def test_waiting_for_a_monitor_sample_does_not_hold_the_vm_lifecycle_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            def read():
                with (state / 'lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 'guest sample'
            metadata = 'State: running\nCPU(s): 4\nMax memory: 8388608 KiB\nAutostart: disable\n'
            with patch.object(vm, 'VM', state), \
                 patch.object(vm, '_vm_observation', (time.monotonic(), {'libvirt_domain.lab': {}}, metadata)), \
                 patch.object(vm._guest_observer, 'read', side_effect=read), \
                 patch.object(vm, 'monitoring_snapshot', return_value=[]):
                self.assertIn('guest sample', vm.observe_snapshot())

    def test_guest_counter_rates_reset_for_a_replaced_container(self):
        spec = importlib.util.spec_from_file_location('guest_observe', vm.SUPPORT / 'infra/guest/observe.py')
        guest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guest)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cpu.stat').write_text('usage_usec 100000\n')
            (root / 'memory.current').write_text('1000\n')
            (root / 'memory.stat').write_text('inactive_file 200\n')
            sampler = guest.ContainerSamples()
            metadata = {'namespace': 'test', 'name': 'pod'}
            sampler.containers = [(metadata, {'name': 'app', 'containerID': 'old'}, root)]
            with patch.object(guest.time, 'monotonic', side_effect=[1, 2, 3]):
                self.assertEqual([{'pods': []}], sampler.sample())
                (root / 'cpu.stat').write_text('usage_usec 350000\n')
                value = sampler.sample()[0]['pods'][0]['containers'][0]
                self.assertEqual(250000000, value['cpu']['usageNanoCores'])
                self.assertEqual(800, value['memory']['workingSetBytes'])
                sampler.containers = [(metadata, {'name': 'app', 'containerID': 'replacement'}, root)]
                self.assertEqual([{'pods': []}], sampler.sample())
            (root / 'cpu.stat').unlink()
            self.assertEqual([{'pods': []}], sampler.sample())

    def test_host_monitoring_reads_counter_deltas_without_launching_docker_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cpu.stat').write_text('usage_usec 100000\n')
            (root / 'memory.current').write_text('1048576\n')
            (root / 'memory.stat').write_text('inactive_file 0\n')
            (root / 'memory.max').write_text('268435456\n')
            with patch.object(vm, '_monitoring_observation', (1, [('id', 'test', root)])), \
                 patch.object(vm, '_monitoring_cpu', {}), \
                 patch.object(vm.time, 'monotonic', side_effect=[1, 1, 2, 2]), \
                 patch.object(vm, 'run') as run:
                self.assertIn('1.0 MiB / 256 MiB', '\n'.join(vm.monitoring_snapshot()))
                (root / 'cpu.stat').write_text('usage_usec 350000\n')
                self.assertIn('25.00%', '\n'.join(vm.monitoring_snapshot()))
                run.assert_not_called()

    def test_monitor_reuses_one_guest_connection_and_closes_only_its_child(self):
        process = unittest.mock.MagicMock()
        process.poll.return_value = None
        process.stdout.readline.side_effect = [json.dumps({'snapshot': 'first'}) + '\n',
                                               json.dumps({'snapshot': 'second'}) + '\n']
        observer = vm.GuestObserver()
        with patch.object(vm.subprocess, 'Popen', return_value=process) as start, \
             patch.object(vm.select, 'select', return_value=([process.stdout], [], [])):
            self.assertEqual('first', observer.read())
            self.assertEqual('second', observer.read())
            start.assert_called_once()
            self.assertIn('sudo python3 -u - --watch', start.call_args.args[0])
            process.stdin.write.assert_called_once()
            observer.close()
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=3)
            self.assertIsNone(observer.process)

    def test_monitor_disconnect_reconnects_without_returning_a_stale_frame(self):
        observer = vm.GuestObserver()
        process = unittest.mock.MagicMock()
        process.poll.return_value = None
        process.stdout.readline.return_value = ''
        with patch.object(vm.subprocess, 'Popen', return_value=process), \
             patch.object(vm.select, 'select', return_value=([process.stdout], [], [])):
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                observer.read()
        self.assertIsNone(observer.process)

    def test_monitor_suppresses_scroll_key_echo_and_restores_terminal(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        original = termios.tcgetattr(slave)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import vm; "
                "vm.observe_snapshot=lambda: 'frame'; vm.OBSERVE_INTERVAL_SECONDS=0.1;\n"
                "try: vm.observe()\n"
                "except KeyboardInterrupt: pass\n")
        child = subprocess.Popen([sys.executable, '-c', code, str(vm.SUPPORT / 'tools')],
                                 stdin=slave, stdout=slave, stderr=slave)
        output = b''
        try:
            deadline = time.monotonic() + 5
            while b'frame' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 8192)
            self.assertIn(b'frame\x1b[K', output)
            self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)
            frames_before_input = output.count(b'frame\x1b[K')
            os.write(master, b'\x1b[B\x1b[A')
            # Exercise input while the observer is active. Interrupting directly
            # after write races the PTY line discipline against ECHO restoration.
            deadline = time.monotonic() + 5
            while output.count(b'frame\x1b[K') <= frames_before_input and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 8192)
            self.assertGreater(output.count(b'frame\x1b[K'), frames_before_input)
            self.assertNotIn(b'^[[B', output)
            self.assertNotIn(b'^[[A', output)
            child.send_signal(signal.SIGINT)
            child.wait(timeout=5)
            while select.select([master], [], [], .1)[0]:
                output += os.read(master, 8192)
            self.assertNotIn(b'^[[B', output)
            self.assertNotIn(b'^[[A', output)
            self.assertEqual(original, termios.tcgetattr(slave))
            self.assertIn(b'\x1b[?1049l', output)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            os.close(master)
            os.close(slave)

    def test_monitor_frame_cannot_scroll_or_wrap_a_small_terminal(self):
        snapshot = '\033[2J' + '\n'.join('x' * 150 for _ in range(100))
        frame = vm.terminal_frame(snapshot, 80, 24)
        self.assertLess(len(frame.splitlines()), 24)
        self.assertTrue(all(len(line) < 80 for line in frame.splitlines()))
        self.assertNotIn('\033', frame)
        self.assertFalse(frame.endswith('\n'))
        self.assertIn('Refresh: 1s', frame)

    def test_start_rejects_replacement_of_persistent_disk(self):
        with self.assertRaisesRegex(RuntimeError, 'replace/delete'):
            vm.check_plan({'resource_changes': [{'address': 'libvirt_volume.system',
                           'change': {'actions': ['delete', 'create']}}]})

    def test_destroy_refuses_another_terraform_resource(self):
        with self.assertRaisesRegex(RuntimeError, 'unowned'):
            vm.check_plan({'resource_changes': [{'address': 'libvirt_domain.other',
                           'change': {'actions': ['delete']}}]}, destroy=True)

    def test_create_and_read_only_changes_are_allowed(self):
        vm.check_plan({'resource_changes': [{'address': 'libvirt_domain.lab',
                       'change': {'actions': ['create']}}]})

    def test_subnet_overlap_detects_even_broader_routes(self):
        with self.assertRaisesRegex(RuntimeError, 'overlaps'):
            vm.overlap([{'dst': '192.168.0.0/16', 'dev': 'vpn0'}])

    def test_subnet_ignores_default_and_owned_bridge_only(self):
        vm.overlap([{'dst': 'default'}, {'dst': vm.CIDR, 'dev': vm.LAB['bridge']},
                    {'dst': '192.168.173.1', 'dev': vm.LAB['bridge']}], owned_bridge=True)
        with self.assertRaises(RuntimeError):
            vm.overlap([{'dst': vm.CIDR, 'dev': 'docker0'}], owned_bridge=True)
        with self.assertRaises(RuntimeError):
            vm.overlap([{'dst': vm.CIDR, 'dev': vm.LAB['bridge']}], owned_bridge=False)

    def test_missing_state_cannot_adopt_an_existing_named_guest(self):
        with patch.object(vm, 'state_resources', return_value={}), \
             patch.object(vm, 'virsh', return_value=subprocess.CompletedProcess([], 0, 'foreign-uuid\n')):
            with self.assertRaisesRegex(RuntimeError, 'collision or lost state'):
                vm.ownership()

    def test_matching_uuid_preserves_ownership(self):
        resources = {f'libvirt_{kind}.lab': {'uuid': 'owned'} for kind in ('domain', 'network', 'pool')}
        with patch.object(vm, 'state_resources', return_value=resources), \
             patch.object(vm, 'virsh', return_value=subprocess.CompletedProcess([], 0, 'owned\n')):
            self.assertEqual(resources, vm.ownership())

    def test_deposed_state_requires_review_instead_of_guessing_owner(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(vm, 'VM', Path(temp)):
            (Path(temp) / 'terraform.tfstate').write_text(json.dumps({'resources': [{
                'type': 'libvirt_domain', 'name': 'lab', 'mode': 'managed', 'instances': [{}, {}]}]}))
            with self.assertRaisesRegex(RuntimeError, 'deposed'):
                vm.state_resources()

    def test_private_state_cannot_be_redirected_outside_lab(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'link'
            path.symlink_to(Path(temp), target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlinked'):
                vm.private_directory(path)

    def test_no_autostart_covers_vm_network_pool_and_host_resume(self):
        resources = {f'libvirt_{kind}.lab': {} for kind in ('domain', 'network', 'pool')}
        with patch.object(vm, 'virsh', return_value=subprocess.CompletedProcess([], 0, vm.NAME + '\n')) as virsh, patch.object(vm, 'run', return_value=subprocess.CompletedProcess([], 1, 'disabled\n')):
            vm.no_autostart(resources)
            self.assertEqual([('autostart', vm.NAME, '--disable'), ('net-autostart', vm.NAME, '--disable'),
                              ('pool-autostart', vm.NAME, '--disable')], [c.args for c in virsh.call_args_list if c.args[-1] == '--disable'])
        with patch.object(vm, 'virsh'), patch.object(vm, 'run', return_value=subprocess.CompletedProcess([], 0, 'enabled\n')):
            with self.assertRaisesRegex(RuntimeError, 'save/resume'):
                vm.no_autostart(resources)


if __name__ == '__main__':
    unittest.main()
