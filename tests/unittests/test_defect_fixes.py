import os
from unittest import mock

from curtin import util
from curtin.block import mdadm
from curtin.commands import block_meta
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
