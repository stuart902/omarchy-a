#!/usr/bin/env python3
"""Require exact new kernel release and installed kernel/modules/DTBs."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import tarfile
import tomllib


def audit_provenance(kernel, pins_path, receipt_path):
    pins = json.loads(pins_path.read_text())
    record = json.loads(receipt_path.read_text())
    expected = {'kernel_commit': pins['kernel_commit'],
                'workflow_commit': pins['orchard_commit'],
                'orchard_commit': pins['upstream_orchard_commit'],
                'build_run': int(pins['hardware_run']),
                'platform': 'apple-16k', 'variant': 'microphone',
                'source_verified': True, 'playback_disabled': True,
                'upstream_tdm_preserved': True,
                'kernel_release': (kernel / 'kernel.release').read_text().strip()}
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError('Hardware provenance mismatch: ' + key)
    for key, path in (('image_sha256', kernel / 'Image.gz'),
                      ('dtb_sha256', kernel / 'dtbs/t8010-j71b.dtb')):
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record[key]:
                raise ValueError('Hardware provenance hash mismatch: ' + key)

    modules = kernel / 'lib/modules' / expected['kernel_release']
    actual_modules = {}
    for path in modules.rglob('*.ko'):
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError('Invalid or empty module: ' + str(path))
        with path.open('rb') as stream:
            actual_modules[path.relative_to(modules).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if not actual_modules or record.get('modules') != actual_modules:
        raise ValueError('Hardware module inventory/hash mismatch')


EXPECTED_RELEASE = '7.3.0-rc1-j71b-audio-nov4-r1-orchard-apple-16k'


def registered_dtbs(orchard):
    if orchard is None:
        raise ValueError('Pinned Orchard checkout is required for a layer audit')
    platform = tomllib.loads((orchard / 'platforms/apple-16k.toml').read_text())
    if platform.get('kind') != 'hardware.platform' or platform.get('name') != 'apple-16k':
        raise ValueError('Invalid pinned platform descriptor')
    boards = {}
    for path in sorted((orchard / 'devices').glob('*/device.toml')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Device descriptor is a symlink')
        device = tomllib.loads(path.read_text())
        kernel = device.get('kernel', {})
        if kernel.get('platform') != 'apple-16k':
            continue
        board, dtb = path.parent.name, kernel.get('dtb')
        if (device.get('kind') != 'hardware.device' or
                device.get('device', {}).get('profile') != board or
                not isinstance(dtb, str) or not re.fullmatch('[a-zA-Z0-9_-]+', dtb)):
            raise ValueError('Invalid registered device/DTB: ' + str(path))
        boards[board] = dtb
    if boards.get('j71b') != 't8010-j71b':
        raise ValueError('Pinned platform does not register the J71b DTB')
    return boards


def audit(kernel, layer=None, orchard=None):
    release = (kernel / 'kernel.release').read_text().strip()
    if release != EXPECTED_RELEASE:
        raise ValueError('Unexpected kernel release: ' + release)
    modules = kernel / 'lib/modules' / release
    found = list(modules.rglob('*.ko'))
    if not found:
        raise ValueError('No kernel modules')
    for module in found:
        vermagic = subprocess.check_output(['modinfo', '-F', 'vermagic', str(module)], text=True).split()
        if not vermagic or vermagic[0] != release:
            raise ValueError('Module vermagic mismatch: ' + str(module))
    if layer:
        prefix = 'usr/lib/modules/' + release + '/'
        expected = {prefix + p.relative_to(modules).as_posix(): p for p in modules.rglob('*') if p.is_file()}
        expected[prefix + 'vmlinuz'] = kernel / 'Image.gz'
        boards = registered_dtbs(orchard)
        expected.update({prefix + 'dtb/' + dtb + '.dtb': kernel / 'dtbs' / (dtb + '.dtb')
                         for dtb in set(boards.values())})
        with tarfile.open(layer, 'r:*') as archive:
            actual = {}
            for member in archive:
                if member.name in actual:
                    raise ValueError('Duplicate layer path')
                actual[member.name] = member
            description = actual.get('usr/lib/orchard/platform.json')
            if description is None or not description.isfile():
                raise ValueError('Missing platform descriptor in layer')
            description = json.load(archive.extractfile(description))
            if description.get('boards') != boards:
                raise ValueError('Installed board map differs from pinned Orchard descriptors')
            for name, source in expected.items():
                entry = actual.get(name)
                if not entry or not entry.isfile():
                    raise ValueError('Missing installed file: ' + name)
                with source.open('rb') as stream:
                    wanted = hashlib.file_digest(stream, 'sha256').digest()
                if hashlib.file_digest(archive.extractfile(entry), 'sha256').digest() != wanted:
                    raise ValueError('Installed file mismatch: ' + name)
            module_files = {name for name in actual if name.startswith('usr/lib/modules/')}
            if module_files != set(expected) | {prefix + 'initrd'}:
                raise ValueError('Unexpected installed module/DTB/kernel entries')
    print('Exact kernel release, all module vermagic, and installed bytes verified')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('kernel', type=Path)
    p.add_argument('--layer', type=Path)
    p.add_argument('--orchard', type=Path)
    p.add_argument('--pins', type=Path)
    p.add_argument('--receipt', type=Path)
    a = p.parse_args()
    if bool(a.pins) != bool(a.receipt): p.error('--pins and --receipt must be paired')
    if a.pins: audit_provenance(a.kernel, a.pins, a.receipt)
    audit(a.kernel, a.layer, a.orchard)
