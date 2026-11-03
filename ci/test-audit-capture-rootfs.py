#!/usr/bin/env python3
"""Synthetic archive checks only; no device or actual-rootfs execution."""
import contextlib
import io
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest


AUDIT = runpy.run_path(str(Path(__file__).with_name('audit-capture-rootfs.py')))['audit']


def regular(data=b'fixture', mode=0o755):
    return tarfile.REGTYPE, data, mode


def link(target, kind=tarfile.SYMTYPE):
    return kind, target, 0o777


def base_files():
    files = {name: regular() for name in (
        'usr/lib/systemd/systemd-udevd', 'usr/bin/kmod', 'usr/bin/pw-record')}
    for unit in ('systemd-udevd.service', 'systemd-udev-trigger.service'):
        files['usr/lib/systemd/system/' + unit] = regular(
            b'[Service]\nExecStart=/usr/bin/fixture\n', 0o644)
    files['usr/lib/udev/rules.d/80-drivers.rules'] = regular(
        b'RUN{builtin}+="kmod load $env{MODALIAS}"\n', 0o644)
    return files


class AuditFixtures(unittest.TestCase):
    def check_archive(self, changes, accepted):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'root.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                for name, (kind, data, mode) in (base_files() | changes).items():
                    member = tarfile.TarInfo(name)
                    member.type, member.mode = kind, mode
                    if kind == tarfile.REGTYPE:
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                    else:
                        member.linkname = data
                        archive.addfile(member)
            with contextlib.redirect_stdout(io.StringIO()):
                if accepted:
                    AUDIT(path)
                else:
                    with self.assertRaises(ValueError):
                        AUDIT(path)

    def test_fixtures(self):
        cases = {
            'baseline': ({}, True),
            'udevd_masked': ({
                'etc/systemd/system/systemd-udevd.service': link('/dev/null')}, False),
            'coldplug_rule_masked': ({
                'etc/udev/rules.d/80-drivers.rules': link('/dev/null')}, False),
            'core_sound_disabled': ({
                'etc/modprobe.d/block.conf': regular(b'install snd_pcm /bin/false\n')}, False),
            'dangling_record_command': ({
                'usr/bin/pw-record': link('missing-pw-cat')}, False),
            'non_executable_record_command': ({
                'usr/bin/pw-record': regular(mode=0o644)}, False),
            'valid_udevd_symlink': ({
                'usr/lib/systemd/systemd-udevd': link('../../bin/udevadm'),
                'usr/bin/udevadm': regular()}, True),
            'valid_record_symlink': ({
                'usr/bin/pw-record': link('pw-cat'),
                'usr/bin/pw-cat': regular()}, True),
            'valid_record_hardlink': ({
                'usr/bin/pw-record': link('usr/bin/pw-cat', tarfile.LNKTYPE),
                'usr/bin/pw-cat': regular()}, True),
            'cyclic_record_link': ({
                'usr/bin/pw-record': link('pw-record')}, False),
            'escaping_record_link': ({
                'usr/bin/pw-record': link('../../../outside')}, False),
            'private_aop_firmware': ({
                'usr/lib/firmware/apple/aop-ipad7b.bin': regular()}, False),
            'unit_without_execstart': ({
                'usr/lib/systemd/system/systemd-udevd.service': regular(b'[Service]\n')}, False),
            'comment_only_rule': ({
                'usr/lib/udev/rules.d/80-drivers.rules': regular(b'# kmod load\n')}, False),
            'unit_dropin_disables_exec': ({
                'etc/systemd/system/systemd-udev-trigger.service.d/override.conf': regular(
                    b'[Service]\nExecStart=\nExecStart=/bin/true\n')}, False),
        }
        for label, (changes, accepted) in cases.items():
            with self.subTest(label=label):
                self.check_archive(changes, accepted)


if __name__ == '__main__':
    unittest.main()
