# This file is part of curtin. See LICENSE file for copyright and license info.
import os

from curtin import paths
from .helpers import CiTestCase


class TestKernelList(CiTestCase):
    def test_kernel_parse_debian_plus(self):
        self.assertEqual(
            paths.kernel_parse('/boot/vmlinuz-6.12.111+deb13-amd64'),
            paths.kernel_parse('/boot/vmlinuz-6.12.111+deb13-arm64'))

    def test_kernel_parse_keeps_old_names(self):
        self.assertGreater(
            paths.kernel_parse('/boot/vmlinuz-6.8.0-48-generic'),
            paths.kernel_parse('/boot/vmlinuz-6.8.0-40-generic'))
        self.assertGreater(
            paths.kernel_parse('/boot/vmlinuz-6.1.0-53-arm64'),
            paths.kernel_parse('/boot/vmlinuz-6.1.0-50-arm64'))

    def test_get_kernel_list_debian(self):
        target = self.tmp_dir()
        boot = os.path.join(target, 'boot')
        os.makedirs(boot)
        for v in ('6.12.107+deb13-amd64', '6.12.111+deb13-amd64'):
            open(os.path.join(boot, 'vmlinuz-' + v), 'w').close()
        got = [v for _, _, v in paths.get_kernel_list(target)]
        self.assertEqual(['6.12.111+deb13-amd64', '6.12.107+deb13-amd64'],
                         got)
