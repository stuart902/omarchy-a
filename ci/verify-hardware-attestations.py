#!/usr/bin/env python3
"""Fail closed on hosted-workflow provenance for every hardware checksum entry."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def verify(directory, commit, output):
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Expected an immutable workflow commit')
    bundle = directory / 'orchard-build.sigstore.jsonl'
    if not bundle.is_file() or bundle.is_symlink() or not bundle.stat().st_size:
        raise ValueError('Missing hardware attestation bundle')
    names = []
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)', line)
        if not match:
            raise ValueError('Invalid hardware checksum entry')
        digest, name = match.groups()
        path = directory / name
        if name in names or not path.is_file() or path.is_symlink():
            raise ValueError('Duplicate or missing hardware input: ' + name)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('Hardware checksum mismatch: ' + name)
        names.append(name)
    if not names:
        raise ValueError('Empty hardware checksum inventory')
    results = {}
    # The checksum inventory is also covered by the producer's wildcard attestation.
    for name in ['SHA256SUMS', *names]:
        command = ['gh', 'attestation', 'verify', str(directory / name),
                   '--bundle', str(bundle), '--repo', 'stuart902/orchard',
                   '--signer-workflow', 'stuart902/orchard/.github/workflows/j71b-audio-current.yml',
                   '--source-digest', commit, '--signer-digest', commit,
                   '--source-ref', 'refs/heads/ci/j71b-audio-current-nov4',
                   '--deny-self-hosted-runners', '--format', 'json']
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        verified = json.loads(result.stdout)
        if not isinstance(verified, list) or not verified:
            raise ValueError('No verified attestations for ' + name)
        results[name] = verified
    output.write_text(json.dumps({'workflow_commit': commit, 'results': results}, indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--commit', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    verify(a.directory, a.commit, a.output)
