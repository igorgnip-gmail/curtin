import argparse
import json
import subprocess
import time
import os
from unittest import mock

from curtin import config, storage_config, util
from curtin.block import mdadm
from curtin.commands import block_meta, collect_logs, in_target
from .helpers import CiTestCase


class TestMdadmCreateFailure(CiTestCase):

    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_subp(cmd, *args, **kwargs):
            self.calls.append(list(cmd))
            if cmd[0] == 'hostname':
                return ('host\n', '')
            if cmd[:2] == ['mdadm', '--create']:
                raise util.ProcessExecutionError(cmd=cmd, exit_code=1)
            return ('', '')

        self.add_patch('curtin.block.mdadm.util.subp', side_effect=fake_subp)
        self.add_patch('curtin.block.mdadm.udev.udevadm_settle')
        self.add_patch('curtin.block.mdadm.assert_valid_devpath')
        self.add_patch('curtin.block.mdadm.get_holders', return_value=[])
        self.add_patch('curtin.block.mdadm.zero_device')

    def test_exec_queue_restarted_and_original_error_kept(self):
        with self.assertRaises(util.ProcessExecutionError):
            mdadm.mdadm_create('/dev/md0', 1, ['/dev/sda1', '/dev/sdb1'])
        self.assertIn(['udevadm', 'control', '--stop-exec-queue'], self.calls)
        self.assertEqual(
            ['udevadm', 'control', '--start-exec-queue'], self.calls[-1])


class TestBlockMetaHandlers(CiTestCase):

    def test_partition_without_device_is_a_value_error(self):
        with self.assertRaises(ValueError):
            block_meta.partition_handler(
                {'id': 'p1', 'size': '1G'}, {}, mock.Mock())

    def _dm_crypt_setup(self, key_paths):
        real_mkstemp = block_meta.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            key_paths.append(path)
            return fd, path

        self.add_patch('curtin.commands.block_meta.tempfile.mkstemp',
                       side_effect=recording_mkstemp)
        self.add_patch(
            'curtin.commands.block_meta.util.load_command_environment',
            return_value={'fstab': None})
        self.add_patch('curtin.commands.block_meta.check_passed_path')
        self.add_patch('curtin.commands.block_meta.get_path_to_storage_volume',
                       return_value='/dev/sda1')
        self.add_patch('curtin.commands.block_meta.dm_crypt_verify')
        self.add_patch('curtin.block.disk_to_byid_path', return_value='/x')
        self.add_patch('curtin.block.zkey_supported', return_value=False)

    def test_dm_crypt_key_file_removed_when_preserved(self):
        paths = []
        self._dm_crypt_setup(paths)
        info = {'id': 'dmc0', 'type': 'dm_crypt', 'volume': 'sda1',
                'key': 'secret', 'preserve': True}
        block_meta.dm_crypt_handler(info, {}, mock.Mock(id_to_device={}))
        self.assertEqual(1, len(paths))
        self.assertFalse(os.path.exists(paths[0]))

    def test_dm_crypt_key_file_removed_when_command_fails(self):
        paths = []
        self._dm_crypt_setup(paths)
        self.add_patch('curtin.commands.block_meta.util.subp',
                       side_effect=util.ProcessExecutionError(
                           cmd=['cryptsetup'], exit_code=1))
        info = {'id': 'dmc0', 'type': 'dm_crypt', 'volume': 'sda1',
                'key': 'secret'}
        with self.assertRaises(util.ProcessExecutionError):
            block_meta.dm_crypt_handler(info, {}, mock.Mock(id_to_device={}))
        self.assertEqual(1, len(paths))
        self.assertFalse(os.path.exists(paths[0]))


class TestBcachePathLookup(CiTestCase):

    def test_backing_device_name_is_matched_exactly(self):
        storage_config = {
            'sda1': {'id': 'sda1', 'type': 'device', 'path': '/dev/sda1'},
            'bc0': {'id': 'bc0', 'type': 'bcache', 'backing_device': 'sda1'},
        }
        self.add_patch('curtin.commands.block_meta.glob.glob',
                       return_value=['/sys/block/bcache1/slaves/sda10',
                                     '/sys/block/bcache0/slaves/sda1'])
        self.add_patch('curtin.commands.block_meta.devsync')
        self.add_patch('curtin.commands.block_meta.block.path_to_kname',
                       side_effect=lambda p: os.path.basename(p))
        self.add_patch('curtin.commands.block_meta.block.kname_to_path',
                       side_effect=lambda k: '/dev/' + k)
        self.assertEqual(
            '/dev/bcache0',
            block_meta.get_path_to_storage_volume('bc0', storage_config))


class TestChrootableTargetCleanup(CiTestCase):

    def setUp(self):
        super().setUp()
        self.target = self.tmp_dir()
        self.etc = os.path.join(self.target, 'etc')
        os.makedirs(self.etc)
        self.resolv = os.path.join(self.etc, 'resolv.conf')
        self.add_patch('curtin.util.do_mount', 'm_mount', return_value=True)
        self.add_patch('curtin.util.do_umount', 'm_umount')
        self.add_patch('curtin.util.log_call')
        self.add_patch('curtin.util.disable_daemons_in_root',
                       return_value=False)

        def fake_copy(src, dst):
            with open(dst, 'w') as fp:
                fp.write('host\n')

        self.add_patch('curtin.util.shutil.copy', side_effect=fake_copy)

    def test_all_mounts_released_after_failed_umount(self):
        with open(self.resolv, 'w') as fp:
            fp.write('orig\n')
        self.m_umount.side_effect = [
            util.ProcessExecutionError(cmd=['umount'], exit_code=32),
            None, None, None]
        with self.assertRaises(util.ProcessExecutionError):
            with util.ChrootableTarget(self.target):
                pass
        self.assertEqual(4, self.m_umount.call_count)
        with open(self.resolv) as fp:
            self.assertEqual('orig\n', fp.read())
        self.assertEqual(['resolv.conf'], os.listdir(self.etc))

    def test_original_error_survives_failed_umount(self):
        self.m_umount.side_effect = util.ProcessExecutionError(
            cmd=['umount'], exit_code=32)
        with self.assertRaises(RuntimeError):
            with util.ChrootableTarget(self.target):
                raise RuntimeError('boom')
        self.assertEqual(4, self.m_umount.call_count)

    def test_host_resolv_conf_not_left_behind(self):
        with util.ChrootableTarget(self.target):
            self.assertTrue(os.path.exists(self.resolv))
        self.assertEqual([], os.listdir(self.etc))

    def test_sys_resolvconf_false_leaves_file_alone(self):
        with open(self.resolv, 'w') as fp:
            fp.write('orig\n')
        with util.ChrootableTarget(self.target, sys_resolvconf=False):
            with open(self.resolv) as fp:
                self.assertEqual('orig\n', fp.read())
        self.assertEqual(['resolv.conf'], os.listdir(self.etc))


class TestInTargetMain(CiTestCase):

    def test_target_from_environment(self):
        args = argparse.Namespace(
            target=None, allow_daemons=False, interactive=False,
            capture=False, command_args=['true'])
        self.add_patch(
            'curtin.commands.in_target.util.load_command_environment',
            return_value={'target': '/t'})
        self.add_patch('curtin.commands.in_target.util.ChrootableTarget',
                       'm_chroot')
        with self.assertRaises(SystemExit) as cm:
            in_target.in_target_main(args)
        self.assertEqual(0, cm.exception.code)
        self.m_chroot.assert_called_with('/t', allow_daemons=False)


class TestSubpPipeline(CiTestCase):

    allowed_subp = True

    def test_failure_of_any_member_is_reported(self):
        with self.assertRaises(util.ProcessExecutionError) as cm:
            util.subp_pipeline([['sh', '-c', 'exit 3'], ['cat']])
        self.assertEqual(3, cm.exception.exit_code)

    def test_failure_of_last_member_is_reported(self):
        with self.assertRaises(util.ProcessExecutionError):
            util.subp_pipeline([['echo', 'x'], ['false']])

    def test_data_flows_through(self):
        out = self.tmp_path('out')
        util.subp_pipeline(
            [['echo', 'hello'], ['tr', 'a-z', 'A-Z'], ['tee', out]])
        with open(out) as fp:
            self.assertEqual('HELLO\n', fp.read())

    def test_missing_program_is_a_process_error(self):
        with self.assertRaises(util.ProcessExecutionError):
            util.subp_pipeline([['echo', 'x'], ['/nonexistent/tool']])

    def test_shell_pipeline_hides_the_failure(self):
        # the behaviour this helper replaces
        self.assertEqual(
            0, subprocess.run(['sh', '-c', 'false | cat']).returncode)


class TestSubpTimeout(CiTestCase):

    allowed_subp = True

    def test_timeout_kills_the_command(self):
        start = time.time()
        with self.assertRaises(util.ProcessExecutionError) as cm:
            util.subp(['sleep', '30'], timeout=0.3)
        self.assertLess(time.time() - start, 10)
        self.assertIn('timed out', str(cm.exception))

    def test_no_timeout_by_default(self):
        self.assertEqual(('', ''), util.subp(['true'], capture=True))


class TestValueAsBoolean(CiTestCase):

    def test_false_strings(self):
        for val in ('no', 'No', 'NO', 'off', 'Off', 'false', 'False', 'none',
                    'None', '0', ''):
            self.assertIs(False, config.value_as_boolean(val), val)

    def test_false_values(self):
        for val in (False, None, 0):
            self.assertIs(False, config.value_as_boolean(val), val)

    def test_true_values(self):
        for val in (True, 1, 'yes', 'on', 'true', 'zero', 'superblock'):
            self.assertIs(True, config.value_as_boolean(val), val)


class TestStorageConfigValidation(CiTestCase):

    @staticmethod
    def _cfg(*items):
        return {'storage': {'version': 1, 'config': list(items)}}

    def test_duplicate_id_is_rejected(self):
        cfg = self._cfg({'id': 'a', 'type': 'disk'},
                        {'id': 'a', 'type': 'disk'})
        with self.assertRaisesRegex(ValueError, 'duplicate storage id'):
            storage_config.extract_storage_ordered_dict(cfg)

    def test_unknown_reference_is_rejected(self):
        cfg = self._cfg({'id': 'a', 'type': 'disk'},
                        {'id': 'a1', 'type': 'partition', 'device': 'nope'})
        storage = storage_config.extract_storage_ordered_dict(cfg)
        with self.assertRaisesRegex(ValueError, 'unknown id'):
            storage_config.validate_references(storage)

    def test_later_reference_is_rejected(self):
        cfg = self._cfg({'id': 'a1', 'type': 'partition', 'device': 'a'},
                        {'id': 'a', 'type': 'disk'})
        storage = storage_config.extract_storage_ordered_dict(cfg)
        with self.assertRaisesRegex(ValueError, 'defined later'):
            storage_config.validate_references(storage)

    def test_ordered_references_pass(self):
        cfg = self._cfg({'id': 'a', 'type': 'disk'},
                        {'id': 'a1', 'type': 'partition', 'device': 'a'})
        storage_config.validate_references(
            storage_config.extract_storage_ordered_dict(cfg))

    def test_dependency_cycle_is_an_error(self):
        cfg = self._cfg({'id': 'p1', 'type': 'partition', 'device': 'p2',
                         'number': 1},
                        {'id': 'p2', 'type': 'partition', 'device': 'p1',
                         'number': 2})
        storage = storage_config.extract_storage_ordered_dict(cfg)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            storage_config.find_item_dependencies('p1', storage)

    def test_bad_config_fails_before_any_device_is_cleared(self):
        cfg = self._cfg({'id': 'a', 'type': 'disk', 'path': '/dev/a'},
                        {'id': 'a', 'type': 'disk', 'path': '/dev/b'})
        args = argparse.Namespace(testmode=True, devices=None,
                                  force_mode=False)
        with mock.patch.object(block_meta.config, 'load_command_config',
                               return_value=cfg), \
                mock.patch.object(block_meta, 'meta_clear') as m_clear:
            self.assertRaises(ValueError, block_meta.block_meta, args)
        m_clear.assert_not_called()

    def test_mounts_are_sorted_by_path_depth(self):
        cfg = self._cfg(
            {'id': 'efi', 'type': 'mount', 'path': '/boot/efi'},
            {'id': 'd', 'type': 'disk'},
            {'id': 'boot', 'type': 'mount', 'path': '/boot'},
            {'id': 'root', 'type': 'mount', 'path': '/'})
        storage = storage_config.extract_storage_ordered_dict(cfg)
        storage.version = 1
        result = block_meta.order_mounts(storage)
        self.assertEqual(['root', 'd', 'boot', 'efi'], list(result))
        self.assertEqual(1, result.version)


class TestPreservedStorage(CiTestCase):

    def test_preserved_bcache_is_recorded_in_device_map(self):
        info = {'id': 'bc', 'type': 'bcache', 'preserve': True,
                'backing_device': 'b', 'cache_device': 'c'}
        context = mock.Mock(id_to_device={})
        with mock.patch.object(block_meta, 'get_path_to_storage_volume',
                               return_value='/dev/bcache0'), \
                mock.patch.object(block_meta, 'bcache_verify',
                                  return_value=True), \
                mock.patch.object(block_meta, 'make_dname'):
            block_meta.bcache_handler(info, {'bc': info}, context)
        self.assertEqual({'bc': '/dev/bcache0'}, context.id_to_device)

    def _format(self, found, fstype):
        info = {'id': 'f', 'type': 'format', 'volume': 'v',
                'fstype': fstype, 'preserve': True}
        with mock.patch.object(block_meta, 'get_path_to_storage_volume',
                               return_value='/dev/sda1'), \
                mock.patch.object(block_meta.block, 'blkid',
                                  return_value={'/dev/sda1': found}):
            block_meta.format_handler(info, {'v': {}, 'f': info}, None)

    def test_preserved_format_mismatch_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "has filesystem 'ext4'"):
            self._format({'TYPE': 'ext4'}, 'xfs')

    def test_preserved_format_match_and_fat_alias_pass(self):
        self._format({'TYPE': 'ext4'}, 'ext4')
        self._format({'TYPE': 'vfat'}, 'fat32')


class TestRedactConfig(CiTestCase):

    def test_secrets_are_blanked_and_returned(self):
        cfg = {
            'storage': {'config': [
                {'id': 'c', 'type': 'dm_crypt', 'key': 'luks-pass'},
                {'id': 'k', 'type': 'disk', 'key': 'not-a-secret'}]},
            'install': {'maas': {'token_secret': 'tsecret'}},
            'reporting': {'hook': {'password': 'hunter2',
                                   'endpoint': 'https://u:s3cr3t@host/x'}},
            'iscsi': ['iscsi:chap:iscsipw@10.0.0.1::3260::iqn.x'],
        }
        out, secrets = collect_logs.redact_config(cfg)
        dumped = json.dumps(out)
        for secret in ('luks-pass', 'tsecret', 'hunter2', 's3cr3t',
                       'iscsipw'):
            self.assertNotIn(secret, dumped)
            self.assertIn(secret, secrets)
        self.assertIn('not-a-secret', dumped)
        self.assertIn('https://u:<REDACTED>@host/x', dumped)
        self.assertEqual('luks-pass',
                         cfg['storage']['config'][0]['key'])

    def test_secrets_are_returned_longest_first(self):
        _, secrets = collect_logs.redact_config(
            {'a': {'password': 'abc'}, 'b': {'password': 'abcdef'}})
        self.assertEqual(['abcdef', 'abc'], secrets)

    def test_tarball_config_has_no_secrets(self):
        cfg = {'storage': {'config': [
            {'id': 'c', 'type': 'dm_crypt', 'key': 'luks-pass'}]}}
        out_dir = self.tmp_dir()
        with mock.patch.object(collect_logs.util, 'subp'), \
                mock.patch.object(collect_logs, '_collect_system_info') as m:
            collect_logs.create_log_tarfile(
                os.path.join(out_dir, 'x.tar'), cfg)
        self.assertNotIn('luks-pass', json.dumps(m.call_args[0][1]))


class TestSubpSecrets(CiTestCase):

    allowed_subp = True

    def _fail(self, script, **kwargs):
        with mock.patch.object(util.LOG, 'debug') as m_debug:
            with self.assertRaises(util.ProcessExecutionError) as ctx:
                util.subp(['sh', '-c', script], capture=True, **kwargs)
        logged = ' '.join(str(c) for c in m_debug.call_args_list)
        return ctx.exception, logged

    def test_secret_is_hidden_in_error_and_logs(self):
        err, logged = self._fail(
            'echo out-sekret; echo err-sekret >&2; exit 3',
            secrets=['sekret'])
        self.assertNotIn('sekret', str(err))
        self.assertNotIn('sekret', logged)
        self.assertIn('<REDACTED>', str(err))

    def test_without_secrets_output_is_kept(self):
        err, _ = self._fail('echo out-sekret; exit 3')
        self.assertIn('out-sekret', str(err))

    def test_error_message_is_bounded_and_full_output_logged(self):
        err, logged = self._fail('head -c 20000 /dev/zero | tr "\\0" x; '
                                 'echo end-marker; exit 1')
        self.assertLess(len(str(err)), util.MAX_ERROR_OUTPUT + 1000)
        self.assertIn('end-marker', str(err))
        self.assertGreater(len(logged), 20000)
