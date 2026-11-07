#!/usr/bin/env python3
"""Strict, opt-in J71b package operations. Never control audio services or recovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import stat
import struct
import subprocess
import sys
import time

RUNTIME = 'usr/lib/omarchy-j71b-microphone/desktop_mic.py'
PLUGIN = 'usr/lib/omarchy-j71b-microphone/j71b-capture.so'
UNIT = 'usr/lib/systemd/user/omarchy-j71b-microphone.service'
RECEIPT = 'etc/omarchy/j71b-microphone.json'
PAYLOAD = (RUNTIME, PLUGIN, UNIT, RECEIPT)
SERVICE = Path(UNIT).name
RAW = 'alsa_input.platform-microphones.stereo-fallback'
NODES = {'capture.j71b.microphone', 'j71b.microphone'}
MAX_AGE = 120
EXPECTED = {'machine': 'aarch64', 'kernel': '7.3.0-rc1-j71b-audio-nov4-r1-orchard-apple-16k',
            'glibc': '2.43', 'python': '3.14.9', 'pipewire': '1.6.10',
            'wireplumber': '0.5.18', 'systemd': '262.1'}
IMAGE = 'preview-audio-nov4-41918815273'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def safe_relative(name):
    path = PurePosixPath(name)
    require(name == str(path) and not path.is_absolute() and '..' not in path.parts,
            'Unsafe package path: ' + name)
    return path


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 16 * 1024 * 1024,
                'Not a bounded, single-link regular file: ' + str(path))
        return source.read()


def elf_abi(data):
    """Inspect the native plugin without executing/loading it or needing binutils."""
    require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01'
            and struct.unpack_from('<HHI', data, 16) == (3, 183, 1),
            'Plugin is not an AArch64 ELF64 little-endian shared object')
    phoff, shoff = struct.unpack_from('<QQ', data, 32)
    phsize, phcount, shsize, shcount = struct.unpack_from('<HHHH', data, 54)
    require(phsize == 56 and shsize == 64 and 0 < phcount < 100 and 0 < shcount < 1000,
            'Unexpected ELF table layout')
    require(phoff + phsize * phcount <= len(data) and shoff + shsize * shcount <= len(data),
            'Truncated ELF tables')
    loads = [struct.unpack_from('<IIQQQQQQ', data, phoff + i * phsize)
             for i in range(phcount)]
    aligns = [p[7] for p in loads if p[0] == 1]
    require(aligns and all(a >= 16384 and a & (a - 1) == 0 for a in aligns),
            'Plugin is not aligned for the 16 KiB target')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shsize)
                for i in range(shcount)]

    def section_bytes(section):
        start, size = section[4:6]
        require(start + size <= len(data), 'Truncated ELF section')
        return data[start:start + size]

    def string(table, offset):
        require(0 <= offset < len(table), 'Invalid ELF string offset')
        end = table.find(b'\0', offset)
        require(end >= 0, 'Unterminated ELF string')
        return table[offset:end].decode('ascii')

    needed, versions, exports = [], set(), set()
    for section in sections:
        if section[1] not in (6, 11, 0x6ffffffe):
            continue
        require(section[6] < len(sections), 'Invalid ELF linked string table')
        table, contents = section_bytes(sections[section[6]]), section_bytes(section)
        if section[1] == 6:
            require(len(contents) % 16 == 0, 'Malformed ELF dynamic table')
            for offset in range(0, len(contents), 16):
                tag, value = struct.unpack_from('<qQ', contents, offset)
                if tag == 1:
                    needed.append(string(table, value))
                require(tag not in (15, 29), 'Plugin must not embed RPATH/RUNPATH')
        elif section[1] == 11:
            require(section[9] == 24 and len(contents) % 24 == 0, 'Malformed ELF symbols')
            for offset in range(0, len(contents), 24):
                name, info, _, index, _, _ = struct.unpack_from('<IBBHQQ', contents, offset)
                if index and info >> 4 in (1, 2):
                    exports.add(string(table, name))
        else:
            offset, visited = 0, set()
            while offset < len(contents):
                require(offset not in visited and offset + 16 <= len(contents), 'Malformed ELF version chain')
                visited.add(offset)
                version, count, _, aux, following = struct.unpack_from('<HHIII', contents, offset)
                require(version == 1 and count < 100, 'Unsupported ELF version requirement')
                cursor = offset + aux
                for _ in range(count):
                    require(cursor + 16 <= len(contents), 'Truncated ELF version requirement')
                    _, _, _, name, next_aux = struct.unpack_from('<IHHII', contents, cursor)
                    versions.add(string(table, name))
                    cursor += next_aux
                if not following:
                    break
                offset += following
    require(sorted(needed) == ['libc.so.6', 'libm.so.6'], 'Unexpected plugin dependency')
    require('ladspa_descriptor' in exports, 'Missing LADSPA entry point')
    require(versions and all(re.fullmatch(r'GLIBC_\d+\.\d+', v) for v in versions),
            'Unexpected native symbol versions')
    highest = max(versions, key=lambda v: tuple(map(int, v[6:].split('.'))))
    require(tuple(map(int, highest[6:].split('.'))) <= (2, 43), 'Plugin requires newer glibc')
    return {'architecture': 'AArch64 ELF64 little-endian shared object',
            'load_segment_alignments': sorted(set(aligns)), 'needed_libraries': sorted(needed),
            'required_symbol_versions': sorted(versions), 'highest_glibc': highest}


def load_package(directory):
    """Verify every byte and refuse extra files/links before accepting a payload."""
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'Package directory required')
    sums = read_regular(directory / 'SHA256SUMS').decode()
    expected = {}
    for line in sums.splitlines():
        require(re.fullmatch(r'[0-9a-f]{64}  .+', line) is not None, 'Malformed SHA256SUMS')
        checksum, name = line.split('  ', 1)
        safe_relative(name)
        require(name not in expected and name != 'SHA256SUMS', 'Duplicate/self checksum entry')
        expected[name] = checksum
    files = {}
    for path in directory.rglob('*'):
        require(not path.is_symlink(), 'Package contains a symlink')
        if path.is_dir():
            continue
        name = path.relative_to(directory).as_posix()
        if name != 'SHA256SUMS':
            files[name] = read_regular(path)
    require(files.keys() == expected.keys(), 'Unexpected/missing package files')
    require(all(digest(data) == expected[name] for name, data in files.items()), 'Package checksum mismatch')
    manifest = json.loads(files['MANIFEST.json'])
    require(manifest['schema'] == 1 and manifest['status'] == 'device-preflight-candidate',
            'Package is not released for read-only device preflight')
    require(manifest['qualification'].get('release_decision') == 'approved-for-device-preflight',
            'Package has no reviewed release decision')
    require(manifest['runtime'] == EXPECTED and manifest['historical_image'] == IMAGE,
            'Unsupported package target')
    payload = {name: files['payload/' + name] for name in PAYLOAD}
    require({name for name in files if name.startswith('payload/')} == {'payload/' + p for p in PAYLOAD},
            'Payload must contain exactly the four reviewed files')
    require(manifest['payload_sha256'] == {p: digest(data) for p, data in payload.items()},
            'Payload manifest mismatch')
    source_sha = digest(files['source/capture.c'])
    require(manifest['source_sha256'] == source_sha, 'Source hash mismatch')
    receipt = {'schema': 1, 'architecture': 'aarch64', 'source': 'experiments/j71b-capture/capture.c',
               'source_sha256': source_sha, 'plugin_sha256': digest(payload[PLUGIN])}
    require(json.loads(payload[RECEIPT]) == receipt, 'Plugin receipt mismatch')
    require(manifest['native_abi'] == elf_abi(payload[PLUGIN]), 'Native ABI record mismatch')
    # Gate records are evidence pointers, not a replacement for reviewed CI results.
    commit = manifest['source_commit']
    require(re.fullmatch('[0-9a-f]{40}', manifest['source_tree']) is not None, 'Full source tree SHA required')
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Full source commit required')
    for gate in ('synthetic_session', 'systemd_manager', 'repository_tests', 'native_dsp'):
        record = manifest['qualification'][gate]
        require(record['commit'] == commit and record['result'] == 'passed'
                and record['url'].startswith('https://github.com/stuart902/omarchy-a/actions/runs/'),
                'Qualification gate is missing, stale or failed: ' + gate)
    return manifest, payload, digest(files['MANIFEST.json'])


def properties(obj):
    return obj.get('info', {}).get('props', obj.get('props', {}))


def running_wireplumber(data, expected_uid=None):
    # WP 0.5.18's client-context.c copies the main core properties, adds
    # wireplumber.client-context=true and appends " [client]" to the name.
    # It is one daemon with two connections, not a second policy manager.
    clients = [properties(obj) for obj in data if obj.get('type') == 'PipeWire:Interface:Client'
               and (str(properties(obj).get('application.name', '')).startswith('WirePlumber')
                    or properties(obj).get('application.process.binary') == 'wireplumber'
                    or properties(obj).get('wireplumber.daemon') is True
                    or properties(obj).get('wireplumber.client-context') is True)]
    require(1 <= len(clients) <= 2, 'Missing/ambiguous running WirePlumber clients')
    identities, credentials = {}, set()
    for props in clients:
        name = props.get('application.name')
        role = {'WirePlumber': 'main', 'WirePlumber [client]': 'client'}.get(name)
        require(role is not None and role not in identities,
                'Missing/ambiguous running WirePlumber main/client roles')
        require(props.get('application.process.binary') == 'wireplumber'
                and props.get('application.version') == EXPECTED['wireplumber']
                and props.get('wireplumber.daemon') is True
                and props.get('wireplumber.profile') == 'main'
                and props.get('wireplumber.client-context', False) is (role == 'client'),
                'Running WirePlumber identity/version/profile/context differs from qualified session')
        # pipewire.sec.* is the server-reported peer identity, unlike the
        # application's self-declared process ID. Both connections must agree.
        peer = tuple(str(props.get('pipewire.sec.' + key, '')) for key in ('pid', 'uid', 'gid'))
        require(all(value.isdecimal() for value in peer) and int(peer[0]) > 0
                and str(props.get('application.process.id', '')) == peer[0],
                'Running WirePlumber lacks a consistent server-reported process identity')
        require(expected_uid is None or int(peer[1]) == expected_uid,
                'Running WirePlumber belongs to a different desktop user')
        credentials.add(tuple(int(value) for value in peer))
        serial = str(props.get('object.serial', ''))
        require(serial.isdecimal() and serial not in identities.values(),
                'Running WirePlumber lacks distinct client serials')
        identities[role] = serial
    require('main' in identities and len(credentials) == 1,
            'Missing/conflicting running WirePlumber daemon identity')
    return identities


def idle_graph(data):
    nodes = [o for o in data if o.get('type') == 'PipeWire:Interface:Node']
    require(not any(properties(n).get('node.name') in NODES for n in nodes), 'Conditioned nodes remain')
    raw = [n for n in nodes if properties(n).get('node.name') == RAW]
    require(len(raw) == 1, 'Missing/ambiguous raw microphone')
    raw = raw[0]
    props = properties(raw)
    required = {'media.class': 'Audio/Source', 'device.api': 'alsa',
                'alsa.card_name': 'J71b Microphones', 'api.alsa.pcm.stream': 'capture'}
    require(all(props.get(k) == v for k, v in required.items())
            and str(props.get('audio.channels')) == '2', 'Wrong raw microphone identity')
    if 'audio.position' in props:
        position = props['audio.position']
        if isinstance(position, str):
            require(re.fullmatch(r'\[\s*(?:"FL"|FL)\s*,?\s*(?:"FR"|FR)\s*\]', position) is not None,
                    'Raw microphone channel positions are not FL/FR')
        else:
            require(position == ['FL', 'FR'], 'Raw microphone channel positions are not FL/FR')
    ports = [properties(obj) for obj in data if obj.get('type') == 'PipeWire:Interface:Port'
             and str(properties(obj).get('node.id', '')) == str(raw['id'])]
    if ports:
        require(len(ports) == 2 and all(p.get('port.direction') == 'out' for p in ports)
                and sorted(p.get('audio.channel', '') for p in ports) == ['FL', 'FR'],
                'Raw microphone ports do not expose exactly FL/FR outputs')
        if any('port.id' in p for p in ports):
            require(all(str(p.get('port.id', '')).isdecimal() for p in ports)
                    and len({int(p['port.id']) for p in ports}) == 2, 'Invalid raw microphone port identities')
            ordered = sorted(ports, key=lambda p: int(p['port.id']))
            require([p['audio.channel'] for p in ordered] == ['FL', 'FR'],
                    'Raw microphone port channel order is not FL/FR')
    require(raw['info'].get('state') in ('idle', 'suspended') and not raw['info'].get('error'),
            'Raw microphone is not healthy and idle')
    serial = str(props.get('object.serial', ''))
    require(serial.isdecimal(), 'Missing raw source serial')
    require(not any(o.get('type') == 'PipeWire:Interface:Link'
                    and o.get('info', {}).get('output-node-id') == raw['id'] for o in data),
            'Raw microphone still has consumer links')
    cores = [o for o in data if o.get('type') == 'PipeWire:Interface:Core']
    require(len(cores) == 1 and cores[0].get('info', {}).get('version') == EXPECTED['pipewire'],
            'Unsupported running PipeWire server')
    return serial


def command(argv, accepted=(0,)):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=8,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    require(result.returncode in accepted and len(result.stdout) <= 16 * 1024 * 1024,
            'Read-only command failed: ' + ' '.join(argv[:3]))
    return result.stdout


def key_values(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def review_audio_kernel(kernel):
    """Reject audio/AOP faults; report two precisely reviewed boot diagnostics.

    The Nov 5 successful capture baseline contains these exact firmware messages
    before AudioController ready and AOP READY. They are not proof of health,
    nor grounds to disregard any other AOP error or later ownership fault.
    """
    lines = kernel.splitlines()
    baseline = (
        r'^apple-aop 210800000\.aop: RTKit: syslog message: BMI262\.cpp:229: BMI262::probe -- Probe error=e00002c0, ID=0x3$',
        r'^apple-aop 210800000\.aop: RTKit: syslog message: spu\.cpp:898: AssertMacros:\s+error: 0$',
    )
    audio_ready = [i for i, line in enumerate(lines)
                   if re.search(r'^apple-aop 210800000\.aop: RTKit: syslog message: AudioController\.cpp:\d+: aop-audio .* ready!!$', line)]
    aop_ready = [i for i, line in enumerate(lines)
                 if line == 'apple-aop 210800000.aop: RTKit: syslog message: SPU_init.cpp:326: ===( AOP IS READY )===']
    notes = []
    for i, line in enumerate(lines):
        if any(re.fullmatch(pattern, line) for pattern in baseline):
            require(any(j > i for j in audio_ready) and any(j > i for j in aop_ready),
                    'AOP boot diagnostic lacks later audio/AOP readiness: ' + line)
            notes.append(line)
            continue
        global_fault = re.search(r'(?i)(kernel panic|\bOops:|\bBUG:|DMA termination.*failed|retaining resources until device reset)', line)
        audio_topic = re.search(r'(?i)(pdm|aop|smartio|apple-sio|microphone|audio)', line)
        fault = re.search(r'(?i)(\bfault\w*|\berror\w*|timeout|timed out|\bhang\w*|\bhung\b|retai|ownership|panic|oops|crash|failed|failure|fatal|abort|did not boot|keeping (?:its )?memory)', line)
        require(not global_fault and not (audio_topic and fault),
                'Audio/AOP fault or kernel failure; stop and preserve evidence: ' + line)
    return notes


def review_kernel_snapshot(kernel, reviewed_sha256):
    """Prove the reviewed prefix; allow only the observed routine motion tail."""
    require(kernel.strip() and re.fullmatch('[0-9a-f]{64}', reviewed_sha256) is not None,
            'A reviewed kernel-log SHA256 is required')
    data = kernel.encode()
    notes = review_audio_kernel(kernel)  # Never hide a fault behind a stale-log error.
    prefix_size = None
    if digest(data) == reviewed_sha256:
        prefix_size = len(data)
    else:
        checksum, size = hashlib.sha256(), 0
        for line in data.splitlines(keepends=True):
            checksum.update(line)
            size += len(line)
            if checksum.hexdigest() == reviewed_sha256:
                prefix_size = size
                break
    require(prefix_size is not None,
            'Current boot log no longer contains the reviewed prefix; review the new log before continuing')
    added = data[prefix_size:].decode().splitlines()
    routine_motion = re.compile(
        r'^apple-aop 210800000\.aop: RTKit: syslog message: CMSPUActivity\.cpp:359: '
        r'\[Activity\] motionState,\d+->\d+,notificationTime,\d+$')
    for line in added:
        require(routine_motion.fullmatch(line) is not None,
                'New kernel diagnostic needs review before continuing: ' + line)
    return {'kernel_sha256': reviewed_sha256, 'kernel_current_sha256': digest(data),
            'kernel_routine_motion_lines': len(added), 'kernel_baseline_diagnostics': notes}


def preflight_binding(report):
    """Benign log growth is evidence, not a change to install eligibility.

    The fresh snapshot has already proved the same reviewed prefix, inspected
    the complete log for audio faults, and allowed only exact motion messages.
    All other package/boot/user/service/source/default/ownership fields bind.
    """
    binding = {key: value for key, value in report.items() if key != 'created_at'}
    observed = dict(binding['observed'])
    for key in ('kernel_current_sha256', 'kernel_routine_motion_lines'):
        observed.pop(key, None)
    binding['observed'] = observed
    return binding


class LiveChecker:
    """Only read operations; used on the tablet only after separate authorization."""
    def __init__(self, username):
        require(os.geteuid() == 0, 'Preflight requires root for readable current kernel logs')
        self.user = pwd.getpwnam(username)
        require(self.user.pw_uid > 0, 'Choose the existing desktop user, never root')

    def user_command(self, argv, accepted=(0,)):
        user, uid = self.user.pw_name, self.user.pw_uid
        env = ['HOME=' + self.user.pw_dir, 'USER=' + user, 'LOGNAME=' + user,
               'PATH=/usr/bin:/bin', 'LANG=C', 'LC_ALL=C', 'XDG_RUNTIME_DIR=/run/user/' + str(uid),
               'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(uid) + '/bus',
               'PIPEWIRE_DAEMON=false', 'PIPEWIRE_CONFIG_DIR=/usr/share/pipewire',
               'PIPEWIRE_CONFIG_PREFIX=/usr/share/pipewire', 'PIPEWIRE_CONFIG_NAME=client.conf']
        return command(['/usr/bin/runuser', '-u', user, '--', '/usr/bin/env', '-i', *env, *argv], accepted)

    def snapshot(self, operation, reviewed_kernel_sha256, image_id):
        require(image_id == IMAGE, 'Operator must verify the historical installed image identity')
        observed = {'machine': platform.machine(), 'kernel': platform.release(),
                    'glibc': os.confstr('CS_GNU_LIBC_VERSION').split()[-1],
                    'python': platform.python_version()}
        for name, binary, pattern in [
                ('pipewire', '/usr/bin/pipewire', r'(?m)^Linked with libpipewire (\S+)$'),
                ('wireplumber', '/usr/bin/wireplumber', r'(?m)^Linked with libwireplumber (\S+)$'),
                ('systemd', '/usr/bin/systemctl', r'^systemd (?:262 \()?([0-9]+\.[0-9]+)')]:
            match = re.search(pattern, command([binary, '--version']))
            require(match is not None, 'Cannot identify ' + name)
            observed[name] = match.group(1)
        require(observed == EXPECTED, 'Installed platform/runtime differs from the qualified target')
        compatible = Path('/proc/device-tree/compatible').read_bytes().decode().rstrip('\0').split('\0')
        require({'apple,j71b', 'apple,t8010'} <= set(compatible), 'Wrong board')
        kernel = command(['/usr/bin/journalctl', '-k', '-b', '--no-pager', '-o', 'cat'])
        kernel_review = review_kernel_snapshot(kernel, reviewed_kernel_sha256)
        for service in ('pipewire.service', 'wireplumber.service'):
            state = self.user_command(['/usr/bin/systemctl', '--user', 'show', service,
                                       '--property=ActiveState,SubState'])
            require(key_values(state) == {'ActiveState': 'active', 'SubState': 'running'},
                    'Existing desktop audio service is not healthy: ' + service)
        environment = key_values(self.user_command(['/usr/bin/systemctl', '--user', 'show-environment']))
        require(not any(k.startswith(('PIPEWIRE_', 'SPA_', 'LD_')) or k == 'LADSPA_PATH'
                        for k in environment), 'Desktop manager has unqualified audio/loader overrides')
        homes = {Path(self.user.pw_dir) / '.local/state'}
        if 'XDG_STATE_HOME' in environment:
            homes.add(Path(environment['XDG_STATE_HOME']))
        for home in homes:
            require(home.is_absolute(), 'Nonabsolute persistent state directory')
            current = home / 'omarchy-j71b-microphone'
            for ancestor in (current, *current.parents):
                require(not ancestor.is_symlink(), 'Persistent state path contains a symlink')
            marker = current / 'ownership.json'
            require(not marker.exists() and not marker.is_symlink(), 'Ownership marker retained; removal is not recovery')
        manager = key_values(self.user_command(['/usr/bin/systemctl', '--user', 'show', '--property=UnitPath,Version']))
        require(re.match(r'^262\.1(?:[- ]|$)', manager.get('Version', '')) is not None,
                'Running user manager differs from the qualified version')
        load_paths = manager.get('UnitPath', '').split()
        require(load_paths and all(p.startswith('/') for p in load_paths), 'Cannot resolve all user unit paths')
        for directory in load_paths:
            for candidate in (Path(directory) / SERVICE, Path(directory) / (SERVICE + '.d'),
                              Path(directory) / 'service.d', Path(directory) / 'omarchy-.service.d',
                              Path(directory) / 'omarchy-j71b-.service.d'):
                if operation == 'remove' and str(candidate) == '/' + UNIT:
                    continue
                require(not candidate.exists() and not candidate.is_symlink(),
                        'Existing user unit, mask or override: ' + str(candidate))
            for relation in ('wants', 'requires', 'upholds'):
                require(not list(Path(directory).glob('*.' + relation + '/' + SERVICE)),
                        'Existing autostart/dependency link for microphone service')
            for alias in Path(directory).glob('*.service'):
                if alias.is_symlink():
                    require(Path(os.readlink(alias)).name != SERVICE, 'Existing microphone unit alias')
        state = key_values(self.user_command(['/usr/bin/systemctl', '--user', 'show', SERVICE,
                          '--property=LoadState,ActiveState,SubState,Result,MainPID,ControlPID,ControlGroup,ExecMainCode,ExecMainStatus,FragmentPath,DropInPaths'], (0, 4)))
        require(state.get('ActiveState') == 'inactive' and state.get('SubState') == 'dead'
                and state.get('MainPID') == '0' and state.get('ControlPID') == '0'
                and state.get('Result', 'success') == 'success'
                and state.get('ExecMainStatus', '0') == '0' and not state.get('DropInPaths'),
                'Service is active, hung, failed or not cleanly stopped')
        require(state.get('LoadState') == ('not-found' if operation == 'install' else 'loaded'),
                'Unexpected service load state; reload metadata separately after authorized changes')
        require(state.get('FragmentPath', '') == ('' if operation == 'install' else '/' + UNIT),
                'Service is loaded from an unexpected file')
        group = state.get('ControlGroup')
        if group:
            require(group.startswith('/') and '..' not in Path(group).parts, 'Unsafe service control group')
            events = Path('/sys/fs/cgroup' + group) / 'cgroup.events'
            if events.exists():
                require('populated 0' in events.read_text().splitlines(), 'Service cgroup still owns processes')
        for proc in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                require(('/' + RUNTIME).encode() not in proc.read_bytes().split(b'\0'),
                        'Microphone supervisor remains running')
            except FileNotFoundError:
                pass
        journal = self.user_command(['/usr/bin/journalctl', '--user', '-b', '-u', SERVICE,
                                     '--no-pager', '-o', 'cat'])
        require(not re.search(r'(?i)(ownership|failed|fault|traceback|error|timed out|timeout|J71b microphone stopped:)', journal),
                'Current-boot service fault evidence remains; no automatic recovery')
        runtime = Path('/run/user/' + str(self.user.pw_uid))
        socket = runtime / 'pipewire-0'
        require(runtime.stat().st_uid == self.user.pw_uid and not runtime.is_symlink()
                and stat.S_ISSOCK(socket.stat().st_mode) and socket.stat().st_uid == self.user.pw_uid
                and not socket.is_symlink(), 'Existing desktop-user PipeWire socket required')
        graph = json.loads(self.user_command(['/usr/bin/pw-dump']))
        serial = idle_graph(graph)
        wireplumber_serials = running_wireplumber(graph, self.user.pw_uid)
        defaults = [o.get('metadata', []) for o in graph if o.get('type') == 'PipeWire:Interface:Metadata'
                    and properties(o).get('metadata.name') == 'default']
        return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'machine_id_sha256': digest(Path('/etc/machine-id').read_bytes()),
                'os_release_sha256': digest(Path('/etc/os-release').read_bytes()),
                'operator_verified_image': image_id, 'hardware_safety_qualified': False,
                **kernel_review,
                'runtime': observed, 'raw_serial': serial, 'wireplumber_client_serials': wireplumber_serials,
                'service': state,
                'default_metadata_sha256': digest(json_bytes(defaults)),
                'user': self.user.pw_name, 'uid': self.user.pw_uid,
                'state_homes': sorted(str(p) for p in homes)}


def target_check(root, payload, operation, owner):
    """Reject links, writable parent paths, ownership drift and partial installs."""
    require(root.is_absolute() and not root.is_symlink(), 'Root must be an absolute real directory')
    for name, data in payload.items():
        path = root / name
        for ancestor in reversed((path.parent, *path.parent.parents)):
            if ancestor != root and root not in ancestor.parents:
                continue
            if not ancestor.exists():
                require(not ancestor.is_symlink(), 'Dangling parent link')
                continue
            info = ancestor.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022,
                    'Unsafe target parent: ' + str(ancestor))
        exists = path.exists() or path.is_symlink()
        if operation == 'install':
            require(not exists, 'Refusing existing target: ' + name)
        else:
            require(exists and not path.is_symlink(), 'Missing/linked installed file: ' + name)
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_gid == owner
                    and stat.S_IMODE(info.st_mode) == 0o644 and info.st_nlink == 1
                    and digest(read_regular(path)) == digest(data),
                    'Installed file hash, ownership or mode changed: ' + name)


def preflight(package, operation, checker, kernel_sha, image_id, root=Path('/'), owner=0, now=None):
    manifest, payload, identity = load_package(package)
    target_check(root, payload, operation, owner)
    observed = json.loads(json_bytes(checker.snapshot(operation, kernel_sha, image_id)))
    return {'schema': 1, 'operation': operation, 'package': identity,
            'source_commit': manifest['source_commit'], 'created_at': time.time() if now is None else now,
            'observed': observed}


def apply(package, report, checker, root=Path('/'), owner=0, now=None):
    """No service actions. All gates precede the first payload mutation."""
    now = time.time() if now is None else now
    require(report.get('schema') == 1 and report.get('operation') in ('install', 'remove'), 'Invalid preflight report')
    require(0 <= now - report['created_at'] <= MAX_AGE, 'Preflight is stale/future-dated; review it again')
    operation = report['operation']
    observed = report['observed']
    fresh = preflight(package, operation, checker, observed['kernel_sha256'],
                      observed['operator_verified_image'], root, owner, now)
    require(preflight_binding(fresh) == preflight_binding(report),
            'Device/session/package changed after preflight; stop and review again')
    _, payload, _ = load_package(package)
    # No directory removal: without an extra persistent ownership receipt, we
    # cannot know which pre-existing directories belong to somebody else.
    order = (RUNTIME, PLUGIN, RECEIPT, UNIT) if operation == 'install' else (UNIT, RUNTIME, PLUGIN, RECEIPT)
    for name in order:
        data = payload[name]
        path = root / name
        if operation == 'install':
            path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            # Recheck all parents immediately before exclusive creation.
            target_check(root, {name: data}, operation, owner)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, 'wb') as output:
                os.fchown(output.fileno(), owner, owner)
                os.fchmod(output.fileno(), 0o644)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        else:
            target_check(root, {name: data}, operation, owner)
            path.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if operation == 'install':
        target_check(root, payload, 'remove', owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'preflight', 'install', 'remove'))
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--user', help='Existing desktop username; never root')
    parser.add_argument('--operation', choices=('install', 'remove'))
    parser.add_argument('--reviewed-kernel-sha256', help='Digest of the reviewed current-boot log prefix; only exact routine AOP motion additions are allowed')
    parser.add_argument('--image-id', help='Installed image identity independently checked by the operator')
    parser.add_argument('--preflight', type=Path)
    args = parser.parse_args()
    if args.action == 'verify':
        manifest, _, identity = load_package(args.package)
        print(json_bytes({'package': identity, 'source_commit': manifest['source_commit'],
                          'native_abi': manifest['native_abi']}).decode(), end='')
        return
    require(args.user, '--user is required')
    checker = LiveChecker(args.user)
    if args.action == 'preflight':
        require(args.operation and args.reviewed_kernel_sha256 and args.image_id,
                'Preflight requires operation, reviewed kernel digest and independently checked image identity')
        result = preflight(args.package, args.operation, checker, args.reviewed_kernel_sha256, args.image_id)
        print(json_bytes(result).decode(), end='')
    else:
        require(args.preflight, 'A fresh read-only preflight report is required')
        report = json.loads(read_regular(args.preflight))
        require(report['operation'] == args.action and report['observed']['user'] == args.user,
                'Preflight action/user does not match')
        apply(args.package, report, checker)
        print('Four package files ' + ('installed' if args.action == 'install' else 'removed') +
              '. No service, defaults, drivers or persistent state were changed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, struct.error, subprocess.SubprocessError) as error:
        raise SystemExit('Refused: ' + str(error) + '\nNo automatic cleanup or recovery was attempted. Inspect this reason before continuing; if an audio fault was reported, stop testing and preserve evidence.')
