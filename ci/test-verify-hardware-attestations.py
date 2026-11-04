#!/usr/bin/env python3
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('verify', Path(__file__).with_name('verify-hardware-attestations.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class AttestationTests(unittest.TestCase):
    def check(self, fault=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.bin').write_bytes(b'fixture')
            (root / 'orchard-build.sigstore.jsonl').write_text('fixture bundle')
            digest = hashlib.sha256(b'fixture').hexdigest()
            (root / 'SHA256SUMS').write_text(digest + '  a.bin\n')
            if fault == 'empty': (root / 'SHA256SUMS').write_text('')
            if fault == 'duplicate': (root / 'SHA256SUMS').write_text((digest + '  a.bin\n') * 2)
            if fault == 'escape': (root / 'SHA256SUMS').write_text(digest + '  ../a.bin\n')
            if fault == 'hash': (root / 'a.bin').write_bytes(b'wrong')
            if fault == 'bundle': (root / 'orchard-build.sigstore.jsonl').unlink()
            def run(argv, **kwargs):
                self.assertTrue(kwargs['check'])
                self.assertIn('--deny-self-hosted-runners', argv)
                for flag, value in (('--source-digest', 'a'*40), ('--signer-digest', 'a'*40),
                                    ('--source-ref', 'refs/heads/ci/j71b-audio-current-nov4'),
                                    ('--signer-workflow', 'stuart902/orchard/.github/workflows/j71b-audio-current.yml'),
                                    ('--repo', 'stuart902/orchard')):
                    self.assertEqual(argv[argv.index(flag)+1], value)
                if fault == 'crypto': raise subprocess.CalledProcessError(1, argv)
                return subprocess.CompletedProcess(argv, 0, stdout='[]' if fault == 'no-results' else '[{"verified":true}]')
            output = root / 'verified.json'
            with patch.object(m.subprocess, 'run', side_effect=run) as mocked:
                if fault:
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        m.verify(root, 'a'*40, output)
                    self.assertFalse(output.exists())
                else:
                    m.verify(root, 'a'*40, output)
                    self.assertEqual(mocked.call_count, 2)
                    self.assertEqual(set(json.loads(output.read_text())['results']), {'SHA256SUMS', 'a.bin'})

    def test_verified_inventory(self): self.check()
    def test_fail_closed(self):
        for fault in ('empty', 'duplicate', 'escape', 'hash', 'bundle', 'crypto', 'no-results'):
            with self.subTest(fault=fault): self.check(fault)

if __name__ == '__main__': unittest.main()
