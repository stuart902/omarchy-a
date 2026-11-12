#!/usr/bin/env python3
"""Build a qualified, non-autostart J71b device-preflight archive (never install it)."""
import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

import j71b_microphone_package as package

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/stuart902/omarchy-a'
SOURCE = 'experiments/j71b-capture/capture.c'


def native_files(archive, expected_sha):
    data = archive.read_bytes()
    package.require(package.digest(data) == expected_sha, 'Native artifact ZIP digest mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        entries = zipped.infolist()
        package.require(len(entries) == 1 and entries[0].filename == 'j71b-microphone-test-aarch64.tar.gz'
                        and entries[0].file_size <= 16 * 1024 * 1024, 'Unexpected native ZIP contents')
        inner = zipped.read(entries[0])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(inner), mode='r:gz') as source:
        for member in source:
            name = PurePosixPath(member.name)
            package.require(not name.is_absolute() and '..' not in name.parts
                            and name.parts[0] == 'j71b-microphone-test', 'Unsafe native archive member')
            if member.isdir():
                continue
            package.require(member.isfile() and len(name.parts) == 2 and member.size <= 16 * 1024 * 1024
                            and name.name not in files, 'Unexpected native archive member')
            files[name.name] = source.extractfile(member).read()
    sums = files.pop('SHA256SUMS').decode().splitlines()
    expected = {}
    for line in sums:
        package.require(re.fullmatch('[0-9a-f]{64}  [A-Za-z0-9_.-]+', line) is not None,
                        'Malformed native checksum list')
        checksum, name = line.split('  ', 1)
        package.require(name not in expected, 'Duplicate native checksum entry')
        expected[name] = checksum
    package.require(files.keys() == expected.keys()
                    and all(package.digest(data) == expected[name] for name, data in files.items()),
                    'Native artifact inner checksum mismatch')
    return files


def tracked_bytes(project, commit, name):
    committed = subprocess.check_output(['git', 'show', commit + ':' + name], cwd=project)
    package.require((project / name).read_bytes() == committed, 'Uncommitted package input: ' + name)
    return committed


def build_files(project, native, zip_sha, qualification):
    """Pure output mapping after trusted local source/CI evidence verification."""
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=project, text=True).strip()
    package.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=project).strip(),
                    'Commit all reviewed changes before packaging; source tree must be clean')
    info = native['BUILD-INFO.txt'].decode()
    package.require('Source commit: ' + commit in info.splitlines()
                    and 'Repository: ' + REPOSITORY in info.splitlines()
                    and 'Architecture: aarch64' in info.splitlines(),
                    'Native artifact must have been built by final-head CI for this repository')
    source = tracked_bytes(project, commit, SOURCE)
    package.require(native['capture.c'] == source, 'Native artifact source is not final source')
    runtime = tracked_bytes(project, commit, 'system/overlay/' + package.RUNTIME)
    unit = tracked_bytes(project, commit, 'system/overlay/' + package.UNIT)
    required_unit = ('Restart=no', 'SendSIGKILL=no', 'KillMode=mixed',
                     'Requisite=pipewire.service wireplumber.service')
    package.require(all(value in unit.decode().splitlines() for value in required_unit)
                    and not re.search(r'(?m)^\[Install\]', unit.decode()), 'Unsafe unit contract')
    plugin = native['j71b-capture.so']
    receipt = {'schema': 1, 'architecture': 'aarch64', 'source': SOURCE,
               'source_sha256': package.digest(source), 'plugin_sha256': package.digest(plugin)}
    payload = {package.RUNTIME: runtime, package.UNIT: unit, package.PLUGIN: plugin,
               package.RECEIPT: package.json_bytes(receipt)}
    manifest = {
        'schema': 1, 'status': 'device-preflight-candidate', 'source_repository': REPOSITORY,
        'source_commit': commit,
        'source_tree': subprocess.check_output(['git', 'rev-parse', commit + '^{tree}'], cwd=project, text=True).strip(),
        'source_sha256': package.digest(source),
        'native_artifact_zip_sha256': zip_sha, 'native_abi': package.elf_abi(plugin),
        'binary_origin': 'Final-head native ARM64 Makefile artifact; not the discarded rootfs-helper binary.',
        'payload_sha256': {name: package.digest(data) for name, data in payload.items()},
        'runtime': package.EXPECTED, 'historical_image': package.IMAGE,
        'historical_rootfs': 'preview-microphone-desktop-41717404430',
        'fresh_device_preflight': 'Not performed by packaging. Separate device authorization and fresh evidence required.',
        'qualification': qualification,
    }
    # This is a deliberate gate, not an automatic declaration that CI passed.
    package.require(qualification.get('release_decision') == 'approved-for-device-preflight',
                    'A reviewed qualification/release decision is required')
    for gate in ('synthetic_session', 'systemd_manager', 'repository_tests', 'native_dsp'):
        record = qualification[gate]
        package.require(record['commit'] == commit and record['result'] == 'passed'
                        and record['url'].startswith(REPOSITORY + '/actions/runs/'),
                        'Missing, failed or stale qualification: ' + gate)
    files = {'payload/' + name: data for name, data in payload.items()}
    files.update({'MANIFEST.json': package.json_bytes(manifest), 'BUILD-INFO.txt': native['BUILD-INFO.txt'],
                  'source/capture.c': source})
    for dest, source_name in [
            ('package.py', 'tools/j71b_microphone_package.py'),
            ('README.md', 'docs/j71b-microphone-package.md'), ('LICENSE', 'LICENSE'),
            ('docs/j71b-microphone.md', 'docs/j71b-microphone.md'),
            ('source/Makefile', 'experiments/j71b-capture/Makefile'),
            ('source/test_capture.c', 'experiments/j71b-capture/test_capture.c'),
            ('source/rootfs-build-helper.py', 'system/microphone.py')]:
        files[dest] = tracked_bytes(project, commit, source_name)
    files['SHA256SUMS'] = ''.join(package.digest(data) + '  ' + name + '\n'
                                  for name, data in sorted(files.items())).encode()
    return files, commit


def write_archive(output, files, commit):
    package.require(not output.exists() and not output.is_symlink(), 'Refusing existing output')
    # Exclusive output; no image build, service or device operation is involved.
    with output.open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            for name, data in sorted(files.items()):
                member = tarfile.TarInfo('j71b-microphone-' + commit[:12] + '/' + name)
                member.size, member.mode, member.uid, member.gid = len(data), 0o644, 0, 0
                member.mtime = 0
                archive.addfile(member, io.BytesIO(data))
    return package.digest(output.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-zip', required=True, type=Path)
    parser.add_argument('--native-zip-sha256', required=True)
    parser.add_argument('--qualification', required=True, type=Path,
                        help='Reviewed final-head gate records; see docs/j71b-microphone-package.md')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    native = native_files(args.native_zip, args.native_zip_sha256)
    files, commit = build_files(PROJECT, native, args.native_zip_sha256,
                                json.loads(args.qualification.read_text()))
    checksum = write_archive(args.output, files, commit)
    print('Source commit: ' + commit + '\nSHA256: ' + checksum + '\n' + str(args.output))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit('Packaging refused: ' + str(error))
