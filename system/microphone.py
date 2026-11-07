"""Build and verify the opt-in J71b LADSPA conditioner from its reviewed source."""
import argparse
import hashlib
import json
import platform
from pathlib import Path, PurePosixPath
import struct
import subprocess
import tarfile

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = 'experiments/j71b-capture/capture.c'
PLUGIN = 'usr/lib/omarchy-j71b-microphone/j71b-capture.so'
RECEIPT = 'etc/omarchy/j71b-microphone.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_elf(data):
    # ELF64, little endian, current version, shared object, EM_AARCH64.
    if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HHI', data, 16) != (3, 183, 1)):
        raise ValueError('J71b microphone plugin must be an AArch64 ELF shared object')


def verify_files(files, project=PROJECT):
    if files.keys() != {PLUGIN, RECEIPT}:
        raise ValueError('Base root lacks the J71b microphone plugin or receipt; rebuild the base')
    data = files[PLUGIN]
    verify_elf(data)
    record = json.loads(files[RECEIPT])
    expected = {'schema': 1, 'architecture': 'aarch64', 'source': SOURCE,
                'source_sha256': digest((project / SOURCE).read_bytes()),
                'plugin_sha256': digest(data)}
    if record != expected:
        raise ValueError('Stale or corrupt J71b microphone plugin receipt; rebuild the base')


def verify_archive(archive, project=PROJECT):
    files = {}
    with tarfile.open(archive, 'r|gz') as source:
        for member in source:
            name = str(PurePosixPath(member.name))
            if name not in (PLUGIN, RECEIPT):
                continue
            if name in files or not member.isfile() or member.size > 16 * 1024 * 1024:
                raise ValueError('Invalid J71b microphone archive member: ' + name)
            files[name] = source.extractfile(member).read()
    verify_files(files, project)


def build(root, project=PROJECT):
    if platform.machine() != 'aarch64':
        raise ValueError('J71b microphone compilation requires a native AArch64 builder')
    destination = root / PLUGIN
    destination.parent.mkdir(parents=True, exist_ok=True)
    # No fast-math: the DSP explicitly detects non-finite inputs. No build
    # dependency is installed into the target filesystem.
    subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-fPIC', '-shared', '-Wl,-z,defs', '-o', str(destination),
                    str(project / SOURCE), '-lm'], check=True)
    data = destination.read_bytes()
    verify_elf(data)
    destination.chmod(0o644)
    receipt = root / RECEIPT
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({'schema': 1, 'architecture': 'aarch64', 'source': SOURCE,
                                  'source_sha256': digest((project / SOURCE).read_bytes()),
                                  'plugin_sha256': digest(data)}, sort_keys=True, indent=2) + '\n')
    receipt.chmod(0o644)
    verify_files({PLUGIN: data, RECEIPT: receipt.read_bytes()}, project)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    build(parser.parse_args().root)
