"""Native microphone build contract and stale base rejection (no device required)."""
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from system import microphone as mic


def plugin_files():
    # Structural ELF fixture only: this does not stand in for a native DSP build.
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, 3, 183, 1)
    data = bytes(data)
    return {mic.PLUGIN: data, mic.RECEIPT: json.dumps({
        'schema': 1, 'architecture': 'aarch64', 'source': mic.SOURCE,
        'source_sha256': mic.digest((mic.PROJECT / mic.SOURCE).read_bytes()),
        'plugin_sha256': mic.digest(data)}).encode()}


class MicrophonePackaging(unittest.TestCase):
    def verify(self, files):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'base.gz'
            with tarfile.open(archive, 'w:gz') as target:
                for name, data in files.items():
                    member = tarfile.TarInfo('./' + name)
                    member.size = len(data)
                    target.addfile(member, io.BytesIO(data))
            mic.verify_archive(archive)

    def test_current_plugin_and_receipt_are_accepted(self):
        self.verify(plugin_files())

    def test_old_base_and_missing_members_are_rejected(self):
        for files in ({}, {mic.PLUGIN: plugin_files()[mic.PLUGIN]},
                      {mic.RECEIPT: plugin_files()[mic.RECEIPT]}):
            with self.subTest(files=list(files)), self.assertRaisesRegex(ValueError, 'rebuild the base'):
                self.verify(files)

    def test_source_and_plugin_hashes_are_checked(self):
        for key in ('source_sha256', 'plugin_sha256'):
            files = plugin_files()
            record = json.loads(files[mic.RECEIPT])
            record[key] = '0' * 64
            files[mic.RECEIPT] = json.dumps(record).encode()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Stale or corrupt'):
                self.verify(files)

    def test_wrong_elf_is_rejected_even_with_matching_hash(self):
        for offset, value in ((4, 1), (5, 2), (16, 2), (18, 62)):
            files = plugin_files()
            data = bytearray(files[mic.PLUGIN])
            data[offset] = value
            files[mic.PLUGIN] = bytes(data)
            record = json.loads(files[mic.RECEIPT])
            record['plugin_sha256'] = mic.digest(data)
            files[mic.RECEIPT] = json.dumps(record).encode()
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, 'AArch64 ELF'):
                self.verify(files)

    def test_link_and_duplicate_members_are_rejected(self):
        for kind in ('link', 'duplicate'):
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'base.gz'
                with tarfile.open(archive, 'w:gz') as target:
                    for name, data in plugin_files().items():
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        target.addfile(member, io.BytesIO(data))
                    member = tarfile.TarInfo(mic.PLUGIN)
                    if kind == 'link':
                        member.type, member.linkname = tarfile.SYMTYPE, '/elsewhere'
                    target.addfile(member)
                with self.assertRaisesRegex(ValueError, 'Invalid J71b'):
                    mic.verify_archive(archive)

    def test_non_native_build_fails_before_compilation(self):
        with patch.object(mic.platform, 'machine', return_value='x86_64'), \
                patch.object(mic.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'native AArch64'):
                mic.build(Path('/unused'))
            run.assert_not_called()

    def test_builder_installs_plugin_and_verified_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def compile_fixture(command, check):
                self.assertTrue(check)
                self.assertEqual(command[0], 'gcc')
                self.assertIn(str(mic.PROJECT / mic.SOURCE), command)
                self.assertNotIn('-ffast-math', command)
                Path(command[command.index('-o') + 1]).write_bytes(plugin_files()[mic.PLUGIN])
            with patch.object(mic.platform, 'machine', return_value='aarch64'), \
                    patch.object(mic.subprocess, 'run', side_effect=compile_fixture):
                mic.build(root)
            files = {name: (root / name).read_bytes() for name in (mic.PLUGIN, mic.RECEIPT)}
            mic.verify_files(files)
            self.assertEqual((root / mic.PLUGIN).stat().st_mode & 0o777, 0o644)

    def test_public_root_rejects_old_base_before_other_dependencies(self):
        from release.public_root import build
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'base.gz'
            with tarfile.open(archive, 'w:gz'):
                pass
            receipt = root / 'base.json'
            receipt.write_text(json.dumps({'bytes': archive.stat().st_size,
                                           'sha256': mic.digest(archive.read_bytes())}))
            with self.assertRaisesRegex(ValueError, 'lacks the J71b'):
                build(archive, receipt, root / 'output', 'test', root / 'missing')
            self.assertFalse((root / 'output').exists())

    def test_build_inputs_and_build_only_dependencies(self):
        from tools.build import INPUTS
        self.assertIn(mic.SOURCE, INPUTS['base'])
        self.assertIn('system/microphone.py', INPUTS['base'])
        packages = (mic.PROJECT / 'system/packages.txt').read_text().splitlines()
        self.assertNotIn('gcc', packages)
        self.assertNotIn('ladspa', packages)
        self.assertIn('gcc ladspa', (mic.PROJECT / 'system/install-toolchain').read_text())
        self.assertIn('-v "$project:/repo:ro"', (mic.PROJECT / 'system/build-rootfs').read_text())


if __name__ == '__main__':
    unittest.main()
