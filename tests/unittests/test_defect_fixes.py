

from curtin import util
from curtin.block import mdadm
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
