#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('assembler', Path(__file__).with_name('assemble-audio-desktop.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class RootReuseTests(unittest.TestCase):
    def fixture(self, directory):
        source = Path(directory) / 'source'
        source.mkdir()
        (source / 'rootfs.tar.zst').write_bytes(b'fixture public root')
        record = m.receipt(source / 'rootfs.tar.zst') | dict(compression='zstd', expanded_bytes=50, members=2)
        (source / 'rootfs.json').write_text(json.dumps(record))
        (source / 'pongo.bin').write_bytes(b'fixture pongo')
        m.write(source, 'old-desktop', record, {'apple-16k': {'boards': ['j71b'], 'layer': {}, 'boot': {}}}, {'commit': 'a' * 40, 'modified': False})
        return source, Path(directory) / 'output'

    def test_exact_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = self.fixture(directory)
            m.reuse_root(source, output)
            for name in ('rootfs.tar.zst', 'rootfs.json'):
                self.assertEqual((source / name).read_bytes(), (output / name).read_bytes())
            proof = json.loads((output / 'rootfs-reuse-proof.json').read_text())
            self.assertEqual(proof['source_root'], proof['output_root'])

    def test_mutated_archive_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = self.fixture(directory)
            (source / 'rootfs.tar.zst').write_bytes(b'changed')
            with self.assertRaises(ValueError): m.reuse_root(source, output)

    def test_mutated_metadata_rejected(self):
        for key in ('bytes', 'sha256', 'compression', 'expanded_bytes', 'members'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                source, output = self.fixture(directory)
                record = json.loads((source / 'rootfs.json').read_text())
                record[key] = 'wrong'
                (source / 'rootfs.json').write_text(json.dumps(record))
                with self.assertRaises(ValueError): m.reuse_root(source, output)

    def test_recovery_gets_new_version_without_rewriting_root_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = self.fixture(directory)
            root = Path(directory)
            hardware = root / 'hardware'
            (hardware / 'components/quiet').mkdir(parents=True)
            (hardware / 'components/quiet/pongo.bin').write_bytes(b'pongo')
            def layer(args, name, kernel):
                (output / '.work').mkdir()
                return {'boards': ['j71b']}
            def recovery(args, name, kernel, boards, record):
                self.assertEqual(json.loads(record.read_text())['release'], 'new-package')
                image = output / 'recovery-apple-16k.bin'
                image.write_bytes(b'recovery')
                return image
            argv = ['assembler', '--source-release', str(source), '--hardware', str(hardware),
                    '--base', str(root / 'base'), '--base-receipt', str(root / 'base.json'),
                    '--logo', str(root / 'logo'), '--orchard', str(root), '--output', str(output),
                    '--release', 'new-package']
            with patch.object(m.sys, 'argv', argv), patch.object(m.snapshot, 'directory', return_value=root), \
                 patch.object(m.snapshot, 'revision', return_value={'commit': 'a'*40, 'modified': False}), \
                 patch.object(m.snapshot, 'dtbs', return_value={'j71b': 't8010-j71b'}), \
                 patch.object(m.build, 'layer', side_effect=layer), patch.object(m.build, 'recovery', side_effect=recovery):
                m.main()
            self.assertEqual((source / 'rootfs.json').read_bytes(), (output / 'rootfs.json').read_bytes())
            self.assertEqual(m.read_release(output / 'release.toml')['release'], 'new-package')

    def test_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = self.fixture(directory)
            output.mkdir()
            with self.assertRaises(FileExistsError): m.reuse_root(source, output)

if __name__ == '__main__': unittest.main()
