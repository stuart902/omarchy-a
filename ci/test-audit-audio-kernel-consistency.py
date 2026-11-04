#!/usr/bin/env python3
import importlib.util
import io
import json
import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('audit', Path(__file__).with_name('audit-audio-kernel-consistency.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
RELEASE = '7.3.0-rc1-j71b-audio-nov4-r1-orchard-apple-16k'

class ConsistencyTests(unittest.TestCase):
    def check(self, fault=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orchard = root / 'orchard'
            (orchard / 'platforms').mkdir(parents=True)
            (orchard / 'platforms/apple-16k.toml').write_text('kind = "hardware.platform"\nname = "apple-16k"\n')
            for board, dtb in (('j71b', 't8010-j71b'), ('d10', 't8010-d10')):
                device = orchard / 'devices' / board
                device.mkdir(parents=True)
                (device / 'device.toml').write_text(f'kind = "hardware.device"\n[device]\nprofile = "{board}"\n[kernel]\nplatform = "apple-16k"\ndtb = "{dtb}"\n')
            kernel = root / 'kernel'
            modules = kernel / 'lib/modules' / RELEASE
            modules.mkdir(parents=True)
            (kernel / 'dtbs').mkdir()
            (kernel / 'kernel.release').write_text(RELEASE)
            (kernel / 'Image.gz').write_bytes(b'kernel')
            (kernel / 'dtbs/t8010-j71b.dtb').write_bytes(b'dtb')
            (kernel / 'dtbs/t8010-d10.dtb').write_bytes(b'd10')
            (kernel / 'dtbs/s8000-n71.dtb').write_bytes(b'unregistered compiled dtb')
            (modules / 'a.ko').write_bytes(b'module')
            prefix = 'usr/lib/modules/' + RELEASE + '/'
            files = {prefix + 'a.ko': b'module', prefix + 'vmlinuz': b'kernel',
                     prefix + 'dtb/t8010-j71b.dtb': b'dtb', prefix + 'dtb/t8010-d10.dtb': b'd10', prefix + 'initrd': b'initrd',
                     'usr/lib/orchard/platform.json': json.dumps({'boards': {'j71b': 't8010-j71b', 'd10': 't8010-d10'}}).encode()}
            if fault == 'missing-registered': files.pop(prefix + 'dtb/t8010-d10.dtb')
            if fault == 'unexpected-dtb': files[prefix + 'dtb/s8000-n71.dtb'] = b'unregistered compiled dtb'
            if fault == 'wrong-board-map': files['usr/lib/orchard/platform.json'] = b'{"boards": {"j71b": "t8010-j71b"}}'
            if fault == 'extra': files[prefix + 'unexpected.ko'] = b'extra'
            if fault in ('a.ko', 'vmlinuz', 'dtb/t8010-j71b.dtb', 'dtb/t8010-d10.dtb'): files[prefix + fault] = b'wrong'
            layer = root / 'layer.tar.gz'
            with tarfile.open(layer, 'w:gz') as tar:
                for name, data in files.items():
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    tar.addfile(item, io.BytesIO(data))
            if fault == 'release': (kernel / 'kernel.release').write_text('7.3.0-rc1-wrong')
            if fault == 'missing-platform': (kernel / 'kernel.release').write_text('7.3.0-rc1-j71b-audio-nov4-r1')
            if fault == 'wrong-version': (kernel / 'kernel.release').write_text('7.2.0-j71b-audio-nov4-r1-orchard-apple-16k')
            with patch.object(m.subprocess, 'check_output', return_value=('wrong' if fault == 'vermagic' else RELEASE) + ' SMP'):
                if fault:
                    with self.assertRaises(ValueError): m.audit(kernel, layer, orchard)
                else: m.audit(kernel, layer, orchard)

    def test_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dtbs').mkdir()
            (root / 'Image.gz').write_bytes(b'kernel')
            (root / 'dtbs/t8010-j71b.dtb').write_bytes(b'dtb')
            (root / 'kernel.release').write_text(RELEASE)
            modules = root / 'lib/modules' / RELEASE
            modules.mkdir(parents=True)
            (modules / 'a.ko').write_bytes(b'module')
            pins = {'kernel_commit': 'a'*40, 'orchard_commit': 'b'*40,
                    'upstream_orchard_commit': 'c'*40, 'hardware_run': '123'}
            record = {'kernel_commit': 'a'*40, 'workflow_commit': 'b'*40,
                      'orchard_commit': 'c'*40, 'build_run': 123, 'platform': 'apple-16k',
                      'variant': 'microphone', 'source_verified': True, 'playback_disabled': True,
                      'upstream_tdm_preserved': True, 'kernel_release': RELEASE,
                      'modules': {'a.ko': hashlib.sha256(b'module').hexdigest()},
                      'image_sha256': hashlib.sha256(b'kernel').hexdigest(),
                      'dtb_sha256': hashlib.sha256(b'dtb').hexdigest()}
            (root / 'pins.json').write_text(json.dumps(pins))
            (root / 'receipt.json').write_text(json.dumps(record))
            m.audit_provenance(root, root / 'pins.json', root / 'receipt.json')
            for key in record:
                with self.subTest(key=key):
                    (root / 'receipt.json').write_text(json.dumps(record | {key: 'wrong'}))
                    with self.assertRaises(ValueError):
                        m.audit_provenance(root, root / 'pins.json', root / 'receipt.json')

            (root / 'receipt.json').write_text(json.dumps(record))
            for data in (b'', b'wrong'):
                (modules / 'a.ko').write_bytes(data)
                with self.assertRaises(ValueError):
                    m.audit_provenance(root, root / 'pins.json', root / 'receipt.json')
            (modules / 'a.ko').unlink()
            with self.assertRaises(ValueError):
                m.audit_provenance(root, root / 'pins.json', root / 'receipt.json')

    def test_valid(self): self.check()
    def test_reject_changes(self):
        for fault in ('extra', 'a.ko', 'vmlinuz', 'dtb/t8010-j71b.dtb', 'release', 'missing-platform', 'wrong-version', 'vermagic', 'missing-registered', 'unexpected-dtb', 'wrong-board-map', 'dtb/t8010-d10.dtb'):
            with self.subTest(fault=fault): self.check(fault)

if __name__ == '__main__': unittest.main()
