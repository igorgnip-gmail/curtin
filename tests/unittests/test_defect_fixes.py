import argparse
import os
from unittest import mock

from curtin import util
from curtin.block import mdadm
from curtin.commands import block_meta, in_target
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
