#!/usr/bin/env python3
"""Test-only assembler: reuse an audited public desktop root byte-for-byte."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release import build, snapshot
from release.manifests import read_release, receipt, write


def reuse_root(source, output):
    manifest = read_release(source / 'release.toml')
    record = json.loads((source / 'rootfs.json').read_text())
    actual = receipt(source / 'rootfs.tar.zst')
    for key in ('bytes', 'sha256'):
        if actual[key] != manifest['root'][key] or actual[key] != record[key]:
            raise ValueError('Reused root differs from manifest or receipt: ' + key)
    for key in ('compression', 'expanded_bytes', 'members'):
        if record[key] != manifest['root'][key]:
            raise ValueError('Reused root metadata mismatch: ' + key)
    output.mkdir(parents=True, exist_ok=False)
    for name in ('rootfs.tar.zst', 'rootfs.json'):
        shutil.copyfile(source / name, output / name)
        if receipt(source / name) != receipt(output / name):
            raise ValueError('Reused root copy mismatch: ' + name)
    proof = {'schema': 1, 'mode': 'byte-for-byte-public-root-reuse',
             'source_release': manifest['release'], 'source_root': actual,
             'output_root': receipt(output / 'rootfs.tar.zst'),
             'root_receipt': receipt(output / 'rootfs.json'),
             'note': 'Root os-release remains the original desktop version; new version identifies packaging only.'}
    (output / 'rootfs-reuse-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-release', 'hardware', 'base', 'base-receipt', 'logo', 'orchard', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--release', required=True)
    a = parser.parse_args()
    orchard = snapshot.directory(a.orchard)
    revision = snapshot.revision(orchard)
    if not revision or revision['modified']:
        parser.error('A clean pinned Orchard checkout is required')
    tools = a.hardware / 'components'
    kernel = tools / 'kernel'
    args = SimpleNamespace(orchard=orchard, output=a.output, base=a.base,
        base_receipt=a.base_receipt, loader=tools / 'quiet/m1n1.bin', logo=a.logo)
    for name in ('busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw'):
        setattr(args, name, tools / 'boot' / name)
    root = reuse_root(a.source_release, a.output)
    files = build.layer(args, 'apple-16k', kernel)
    boards = files.pop('boards')
    # Recovery needs the new packaging version; preserve the public root receipt.
    recovery_receipt = a.output / '.work/recovery-root.json'
    recovery_receipt.write_text(json.dumps(root | {'release': a.release}) + '\n')
    image = build.recovery(args, 'apple-16k', kernel, snapshot.dtbs(orchard, boards), recovery_receipt)
    platforms = {'apple-16k': {'boards': boards, 'layer': files, 'boot': receipt(image)}}
    shutil.copyfile(tools / 'quiet/pongo.bin', a.output / 'pongo.bin')
    shutil.rmtree(a.output / '.work', ignore_errors=True)
    write(a.output, a.release, root, platforms, revision)
    provenance = {'schema': 2, 'release': a.release, 'orchard': revision, 'platforms': platforms,
                  'root_reuse': json.loads((a.output / 'rootfs-reuse-proof.json').read_text()),
                  'inputs': {name: receipt(path) for name, path in vars(args).items()
                             if isinstance(path, Path) and path.is_file()}}
    (a.output / 'build-receipt.json').write_text(json.dumps(provenance, indent=2) + '\n')


if __name__ == '__main__':
    main()
