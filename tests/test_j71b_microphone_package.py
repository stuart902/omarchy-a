"""Offline package tests. Fake roots/checker only; no device, daemon or socket use."""
from copy import deepcopy
from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import stat
from types import SimpleNamespace
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('j71b_microphone_package', PROJECT / 'tools/j71b_microphone_package.py')
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
spec = importlib.util.spec_from_file_location('package_j71b_microphone', PROJECT / 'tools/package_j71b_microphone.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fake_elf():
    """Nonexecutable synthetic ELF metadata fixture; never loaded as machine code."""
    strings = b'\0libc.so.6\0libm.so.6\0ladspa_descriptor\0GLIBC_2.17\0GLIBC_2.29\0'
    index = lambda text: strings.index(text.encode())
    dynamic = b''.join(struct.pack('<qQ', 1, index(name)) for name in ('libc.so.6', 'libm.so.6')) + bytes(16)
    symbols = struct.pack('<IBBHQQ', index('ladspa_descriptor'), 0x12, 0, 1, 0, 0)
    versions = struct.pack('<HHIII', 1, 2, index('libc.so.6'), 16, 0)
    versions += struct.pack('<IHHII', 0, 0, 0, index('GLIBC_2.17'), 16)
    versions += struct.pack('<IHHII', 0, 0, 0, index('GLIBC_2.29'), 0)
    content = bytearray(64 + 56 + 5 * 64)
    content[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', content, 16, 3, 183, 1)
    struct.pack_into('<QQ', content, 32, 64, 120)
    struct.pack_into('<HHHH', content, 54, 56, 1, 64, 5)
    struct.pack_into('<IIQQQQQQ', content, 64, 1, 5, 0, 0, 0, 0, 0, 65536)
    for num, (kind, chunk, link, stride) in enumerate(((3, strings, 0, 0), (6, dynamic, 1, 16),
                                                     (11, symbols, 1, 24), (0x6ffffffe, versions, 1, 0)), 1):
        offset = len(content)
        content.extend(chunk)
        struct.pack_into('<IIQQQQIIQQ', content, 120 + num * 64, 0, kind, 0, 0, offset, len(chunk), link, 0, 1, stride)
    return bytes(content)


def raw_graph():
    return [{'type': 'PipeWire:Interface:Core', 'info': {'version': '1.6.10'}},
            {'id': 10, 'type': 'PipeWire:Interface:Node', 'info': {'state': 'suspended', 'props': {
                'node.name': pkg.RAW, 'object.serial': 110, 'media.class': 'Audio/Source',
                'device.api': 'alsa', 'alsa.card_name': 'J71b Microphones',
                'api.alsa.pcm.stream': 'capture', 'audio.channels': 2}}},
            {'id': 20, 'type': 'PipeWire:Interface:Client', 'info': {'props': {
                'application.name': 'WirePlumber', 'application.process.binary': 'wireplumber',
                'application.version': '0.5.18', 'wireplumber.daemon': True,
                'wireplumber.profile': 'main', 'object.serial': 120}}}]


def fixture_files():
    plugin, source = fake_elf(), b'/* Offline synthetic source fixture, not a device build. */\n'
    receipt = {'schema': 1, 'architecture': 'aarch64', 'source': builder.SOURCE,
               'source_sha256': pkg.digest(source), 'plugin_sha256': pkg.digest(plugin)}
    payload = {pkg.RUNTIME: b'# offline fake runtime\n', pkg.PLUGIN: plugin,
               pkg.UNIT: b'# offline fake unit\n', pkg.RECEIPT: pkg.json_bytes(receipt)}
    qualification = {gate: {'commit': '1' * 40, 'result': 'passed',
                           'url': builder.REPOSITORY + '/actions/runs/0'}
                     for gate in ('synthetic_session', 'systemd_manager', 'repository_tests', 'native_dsp')}
    manifest = {'schema': 1, 'status': 'device-preflight-candidate', 'source_commit': '1' * 40, 'source_tree': '3' * 40,
                'runtime': pkg.EXPECTED, 'historical_image': pkg.IMAGE,
                'source_sha256': pkg.digest(source), 'native_abi': pkg.elf_abi(plugin),
                'payload_sha256': {name: pkg.digest(data) for name, data in payload.items()},
                'qualification': {**qualification, 'release_decision': 'approved-for-device-preflight'}}
    files = {'payload/' + name: data for name, data in payload.items()}
    files.update({'source/capture.c': source, 'MANIFEST.json': pkg.json_bytes(manifest)})
    return files


def write_package(directory, files):
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (directory / 'SHA256SUMS').write_text(''.join(pkg.digest(data) + '  ' + name + '\n'
                                               for name, data in sorted(files.items())))


class OfflinePackage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package, self.root = self.base / 'package', self.base / 'root'
        self.root.mkdir(mode=0o700)
        self.files = fixture_files()
        write_package(self.package, self.files)
        self.checker = Mock()
        self.checker.snapshot.return_value = {
            'boot_id': 'offline-boot', 'runtime': pkg.EXPECTED.copy(), 'raw_serial': '110',
            'kernel_sha256': '2' * 64, 'operator_verified_image': pkg.IMAGE, 'user': 'offline-user'}
        self.owner = os.getuid()
        # Any accidental real subprocess, including read-only service/socket
        # interaction, makes these fake-root tests fail immediately.
        self.no_process = patch.object(pkg.subprocess, 'run', side_effect=AssertionError('No subprocess in fake-root tests'))
        self.no_process.start()
        self.addCleanup(self.no_process.stop)

    def preflight(self, operation='install', now=1000):
        return pkg.preflight(self.package, operation, self.checker, '2' * 64, pkg.IMAGE,
                             self.root, self.owner, now)

    def apply(self, report=None, now=1001):
        pkg.apply(self.package, report or self.preflight(), self.checker, self.root, self.owner, now)

    def test_valid_package_and_native_abi(self):
        manifest, payload, identity = pkg.load_package(self.package)
        self.assertEqual(set(payload), set(pkg.PAYLOAD))
        self.assertEqual(manifest['native_abi']['highest_glibc'], 'GLIBC_2.29')
        self.assertEqual(manifest['native_abi']['load_segment_alignments'], [65536])
        self.assertEqual(len(identity), 64)

    def test_roundtrip_exact_four_files_and_preserved_state(self):
        state = self.root / 'home/user/.local/state/omarchy-j71b-microphone'
        state.mkdir(parents=True)
        (state / 'lock').write_text('persistent state must survive')
        self.apply()
        self.assertEqual(len([p for p in self.root.rglob('*') if p.is_file()]), 5)
        for name in pkg.PAYLOAD:
            self.assertEqual((self.root / name).read_bytes(), self.files['payload/' + name])
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o644)
        self.apply(self.preflight('remove'))
        self.assertFalse(any((self.root / name).exists() for name in pkg.PAYLOAD))
        self.assertEqual((state / 'lock').read_text(), 'persistent state must survive')
        self.assertTrue((self.root / Path(pkg.RUNTIME).parent).is_dir())

    def test_no_mutation_without_current_passed_preflight(self):
        for age in (-1, pkg.MAX_AGE + 1):
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, 'stale/future'):
                self.apply(self.preflight(), now=1000 + age)
        self.assertFalse(list(self.root.iterdir()))
        for failure in ('ownership marker', 'hardware fault', 'override', 'hung service', 'raw running'):
            self.checker.snapshot.side_effect = ValueError(failure)
            with self.subTest(failure=failure), self.assertRaisesRegex(ValueError, failure):
                self.preflight()
        self.assertFalse(list(self.root.iterdir()))

    def test_changed_boot_source_session_or_hash_blocks_install(self):
        for key, value in [('boot_id', 'new-boot'), ('raw_serial', '111'), ('user', 'other')]:
            report = self.preflight()
            old = deepcopy(self.checker.snapshot.return_value)
            self.checker.snapshot.return_value[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'changed after preflight'):
                self.apply(report)
            self.checker.snapshot.return_value = old
        report = self.preflight()
        (self.package / 'source/capture.c').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.apply(report)
        self.assertFalse(list(self.root.iterdir()))

    def test_refuses_existing_files_and_dangling_symlinks_without_overwrite(self):
        path = self.root / pkg.RUNTIME
        path.parent.mkdir(parents=True)
        path.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'existing target'):
            self.apply()
        self.assertEqual(path.read_bytes(), b'existing')
        path.unlink()
        path.symlink_to(self.base / 'absent')
        with self.assertRaisesRegex(ValueError, 'existing target'):
            self.apply()
        self.assertTrue(path.is_symlink())

    def test_refuses_symlink_or_writable_parent(self):
        (self.root / 'usr').symlink_to(self.base / 'outside')
        with self.assertRaisesRegex(ValueError, 'link'):
            self.apply()
        (self.root / 'usr').unlink()
        (self.root / 'usr').mkdir(mode=0o777)
        (self.root / 'usr').chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'Unsafe target parent'):
            self.apply()

    def test_removal_refuses_drift_before_removing_anything(self):
        self.apply()
        path = self.root / pkg.RECEIPT
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.preflight('remove')
        self.assertTrue(all((self.root / name).exists() for name in pkg.PAYLOAD))

    def test_removal_refuses_mode_links_and_missing_payload(self):
        self.apply()
        path = self.root / pkg.RUNTIME
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.preflight('remove')
        path.chmod(0o644)
        os.link(path, self.root / 'hard-link')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.preflight('remove')
        (self.root / 'hard-link').unlink()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing/linked'):
            self.preflight('remove')

    def test_marker_or_hang_appearing_after_preflight_preserves_files(self):
        self.apply()
        report = self.preflight('remove')
        self.checker.snapshot.side_effect = ValueError('ownership marker retained')
        with self.assertRaisesRegex(ValueError, 'ownership marker'):
            self.apply(report)
        self.assertTrue(all((self.root / name).exists() for name in pkg.PAYLOAD))

    def test_package_rejects_extra_missing_links_and_tampering(self):
        (self.package / 'unexpected').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'Unexpected/missing'):
            pkg.load_package(self.package)
        (self.package / 'unexpected').unlink()
        (self.package / 'unexpected').symlink_to(self.base / 'absent')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            pkg.load_package(self.package)
        (self.package / 'unexpected').unlink()
        (self.package / 'source/capture.c').unlink()
        with self.assertRaisesRegex(ValueError, 'Unexpected/missing'):
            pkg.load_package(self.package)

    def test_rejects_stale_or_failed_gate_even_with_recomputed_checksums(self):
        for change in ({'commit': '0' * 40}, {'result': 'failed'}):
            files = fixture_files()
            manifest = json.loads(files['MANIFEST.json'])
            manifest['qualification']['systemd_manager'].update(change)
            files['MANIFEST.json'] = pkg.json_bytes(manifest)
            write_package(self.package, files)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Qualification'):
                pkg.load_package(self.package)

    def test_receipt_and_abi_must_match_even_with_recomputed_checksums(self):
        for field in ('source_sha256', 'native_abi'):
            files = fixture_files()
            manifest = json.loads(files['MANIFEST.json'])
            manifest[field] = 'not-valid'
            files['MANIFEST.json'] = pkg.json_bytes(manifest)
            write_package(self.package, files)
            with self.subTest(field=field), self.assertRaises(ValueError):
                pkg.load_package(self.package)

    def test_raw_graph_gates_identity_idle_consumers_and_nodes(self):
        self.assertEqual(pkg.idle_graph(raw_graph()), '110')
        bad = []
        graph = raw_graph(); graph[1]['info']['state'] = 'running'; bad.append(graph)
        graph = raw_graph(); graph[1]['info']['props']['audio.channels'] = 1; bad.append(graph)
        graph = raw_graph(); graph[0]['info']['version'] = '1.4.2'; bad.append(graph)
        graph = raw_graph(); graph[1]['info']['error'] = 'fault'; bad.append(graph)
        graph = raw_graph(); graph += [deepcopy(graph[1])]; bad.append(graph)
        graph = raw_graph(); graph += [{'type': 'PipeWire:Interface:Node', 'info': {'props': {'node.name': 'j71b.microphone'}}}]; bad.append(graph)
        graph = raw_graph(); graph += [{'type': 'PipeWire:Interface:Link', 'info': {'output-node-id': 10}}]; bad.append(graph)
        for graph in bad:
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                pkg.idle_graph(graph)


    def test_present_raw_channel_positions_and_ports_are_checked(self):
        for position in ('[ FL, FR ]', '["FL", "FR"]', ['FL', 'FR']):
            graph = raw_graph()
            graph[1]['info']['props']['audio.position'] = position
            self.assertEqual(pkg.idle_graph(graph), '110')
        for position in ('[ FR, FL ]', '[ FL, FL ]', ['FC'], None):
            graph = raw_graph()
            graph[1]['info']['props']['audio.position'] = position
            with self.subTest(position=position), self.assertRaisesRegex(ValueError, 'positions'):
                pkg.idle_graph(graph)
        graph = raw_graph()
        for index, channel in enumerate(('FL', 'FR')):
            graph.append({'type': 'PipeWire:Interface:Port', 'info': {'props': {
                'node.id': 10, 'port.id': index, 'port.direction': 'out', 'audio.channel': channel}}})
        self.assertEqual(pkg.idle_graph(graph), '110')
        graph[-1]['info']['props']['audio.channel'] = 'FL'
        with self.assertRaisesRegex(ValueError, 'ports'):
            pkg.idle_graph(graph)
        graph[-1]['info']['props']['audio.channel'] = 'FR'
        graph[-1]['info']['props']['port.id'] = -1
        with self.assertRaisesRegex(ValueError, 'port identities'):
            pkg.idle_graph(graph)
        graph[-1]['info']['props']['port.id'] = 0
        graph[-2]['info']['props']['port.id'] = 1
        with self.assertRaisesRegex(ValueError, 'port channel order'):
            pkg.idle_graph(graph)

    def test_elf_rejects_wrong_arch_truncation_and_small_alignment(self):
        wrong_arch, small = bytearray(fake_elf()), bytearray(fake_elf())
        struct.pack_into('<H', wrong_arch, 18, 62)
        struct.pack_into('<Q', small, 64 + 48, 4096)
        for data in (b'', fake_elf()[:100], bytes(wrong_arch), bytes(small)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                pkg.elf_abi(data)

    def test_archive_writer_is_exclusive_and_only_four_payload_files(self):
        archive = self.base / 'offline-fixture.tar.gz'
        checksum = builder.write_archive(archive, self.files, '1' * 40)
        self.assertEqual(checksum, pkg.digest(archive.read_bytes()))
        with tarfile.open(archive) as stream:
            payload = [member for member in stream if '/payload/' in member.name]
        self.assertEqual(len(payload), 4)
        self.assertTrue(all(m.uid == 0 and m.gid == 0 and m.mode == 0o644 for m in payload))
        with self.assertRaisesRegex(ValueError, 'existing output'):
            builder.write_archive(archive, self.files, '1' * 40)

    def test_native_reader_checks_inner_and_outer_hashes(self):
        def zipped(tamper=False):
            inner = io.BytesIO()
            files = {'capture.c': b'fixture'}
            files['SHA256SUMS'] = ((('0' * 64) if tamper else pkg.digest(b'fixture')) + '  capture.c\n').encode()
            with tarfile.open(fileobj=inner, mode='w:gz') as stream:
                for name, data in files.items():
                    member = tarfile.TarInfo('j71b-microphone-test/' + name)
                    member.size = len(data)
                    stream.addfile(member, io.BytesIO(data))
            path = self.base / ('native-bad.zip' if tamper else 'native.zip')
            with zipfile.ZipFile(path, 'w') as stream:
                stream.writestr('j71b-microphone-test-aarch64.tar.gz', inner.getvalue())
            return path
        good = zipped()
        self.assertEqual(builder.native_files(good, pkg.digest(good.read_bytes())), {'capture.c': b'fixture'})
        with self.assertRaisesRegex(ValueError, 'ZIP digest'):
            builder.native_files(good, '0' * 64)
        bad = zipped(True)
        with self.assertRaisesRegex(ValueError, 'inner checksum'):
            builder.native_files(bad, pkg.digest(bad.read_bytes()))


class ReadOnlyChecker(unittest.TestCase):
    """Exercise live-checker branches with every command/filesystem read mocked."""
    def setUp(self):
        self.kernel = 'J71b microphone initialized; idle\n'
        self.kernel_sha = pkg.digest(self.kernel.encode())
        self.graph = raw_graph()
        self.exists = set()
        self.links = set()
        self.journal = ''
        self.manager_environment = ''
        self.manager_version = '262.1-1-arch'
        self.operation = 'install'
        self.state = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead',
                      'Result': 'success', 'MainPID': '0', 'ControlPID': '0', 'ControlGroup': '',
                      'ExecMainCode': '0', 'ExecMainStatus': '0', 'FragmentPath': '', 'DropInPaths': ''}
        self.called = []
        self.checker = pkg.LiveChecker.__new__(pkg.LiveChecker)
        self.checker.hardware_healthy = True
        self.checker.user = SimpleNamespace(pw_name='desktop', pw_uid=1000, pw_dir='/home/desktop')
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        for name, value in [('machine', 'aarch64'), ('release', pkg.EXPECTED['kernel']),
                            ('python_version', pkg.EXPECTED['python'])]:
            self.context.enter_context(patch.object(pkg.platform, name, return_value=value))
        self.context.enter_context(patch.object(pkg.os, 'confstr', return_value='glibc 2.43'))
        self.context.enter_context(patch.object(pkg, 'command', side_effect=self.command))
        self.context.enter_context(patch.object(self.checker, 'user_command', side_effect=self.user_command))
        self.context.enter_context(patch.object(Path, 'exists', lambda path: str(path) in self.exists))
        self.context.enter_context(patch.object(Path, 'is_symlink', lambda path: str(path) in self.links))
        self.context.enter_context(patch.object(Path, 'glob', lambda path, pattern: []))
        self.context.enter_context(patch.object(Path, 'read_bytes', lambda path: self.read_bytes(path)))
        self.context.enter_context(patch.object(Path, 'read_text', lambda path: self.read_bytes(path).decode()))
        self.context.enter_context(patch.object(Path, 'stat', lambda path: SimpleNamespace(
            st_uid=1000, st_mode=(stat.S_IFSOCK if path.name == 'pipewire-0' else stat.S_IFDIR) | 0o700)))
        self.context.enter_context(patch.object(pkg.subprocess, 'run', side_effect=AssertionError('No real command allowed')))

    def read_bytes(self, path):
        mapping = {'/proc/device-tree/compatible': b'apple,j71b\0apple,t8010\0',
                   '/proc/sys/kernel/random/boot_id': b'current-boot\n',
                   '/etc/machine-id': b'current-machine\n', '/etc/os-release': b'BUILD_ID=rolling\n'}
        return mapping[str(path)]

    def command(self, argv, accepted=(0,)):
        self.called.append(argv)
        outputs = {'/usr/bin/pipewire': 'Compiled with libpipewire 1.6.10\nLinked with libpipewire 1.6.10\n',
                   '/usr/bin/wireplumber': 'Compiled with libwireplumber 0.5.18\nLinked with libwireplumber 0.5.18\n',
                   '/usr/bin/systemctl': 'systemd 262 (262.1-1-arch)\n',
                   '/usr/bin/journalctl': self.kernel}
        return outputs[argv[0]]

    def user_command(self, argv, accepted=(0,)):
        self.called.append(argv)
        if argv[0] == '/usr/bin/pw-dump':
            return json.dumps(self.graph)
        if argv[0] == '/usr/bin/journalctl':
            return self.journal
        if argv[-1] == 'show-environment':
            return self.manager_environment
        if argv[-1] == '--property=UnitPath,Version':
            return 'UnitPath=/etc/systemd/user /usr/lib/systemd/user\nVersion=' + self.manager_version + '\n'
        if 'pipewire.service' in argv or 'wireplumber.service' in argv:
            return 'ActiveState=active\nSubState=running\n'
        if pkg.SERVICE in argv:
            return ''.join(key + '=' + value + '\n' for key, value in self.state.items())
        raise AssertionError('Unexpected read command: ' + repr(argv))

    def snapshot(self):
        return self.checker.snapshot(self.operation, self.kernel_sha, pkg.IMAGE)

    def test_unconfirmed_hardware_health_refuses_without_device_reads(self):
        self.checker.hardware_healthy = False
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            self.snapshot()
        self.assertFalse(self.called)

    def test_complete_fresh_snapshot_only_reads(self):
        self.assertEqual(self.snapshot()['raw_serial'], '110')
        forbidden = {'start', 'stop', 'enable', 'restart', 'daemon-reload', 'kill', 'reset-failed'}
        self.assertTrue(all(not forbidden.intersection(argv) for argv in self.called))


    def test_running_wireplumber_exact_version_and_identity(self):
        self.assertEqual(self.snapshot()['wireplumber_client_serial'], '120')
        for key, value in [('application.version', '0.5.17'), ('application.name', 'Other manager'),
                           ('application.process.binary', 'other'), ('wireplumber.daemon', False),
                           ('wireplumber.profile', 'custom'), ('object.serial', '')]:
            props = self.graph[2]['info']['props']
            previous = props[key]
            props[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'WirePlumber'):
                self.snapshot()
            props[key] = previous

    def test_missing_or_duplicate_running_wireplumber_refuses(self):
        client = self.graph.pop()
        with self.assertRaisesRegex(ValueError, 'Missing/ambiguous running WirePlumber'):
            self.snapshot()
        self.graph.extend([client, deepcopy(client)])
        with self.assertRaisesRegex(ValueError, 'Missing/ambiguous running WirePlumber'):
            self.snapshot()

    def test_stale_kernel_review_and_current_fault_refuse(self):
        self.kernel += 'aop resource ownership retained\n'
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            self.snapshot()
        self.kernel_sha = pkg.digest(self.kernel.encode())
        with self.assertRaisesRegex(ValueError, 'Hardware fault'):
            self.snapshot()

    def test_marker_and_all_relevant_override_paths_refuse(self):
        for path in ('/home/desktop/.local/state/omarchy-j71b-microphone/ownership.json',
                     '/etc/systemd/user/' + pkg.SERVICE,
                     '/etc/systemd/user/' + pkg.SERVICE + '.d',
                     '/etc/systemd/user/omarchy-j71b-.service.d',
                     '/etc/systemd/user/omarchy-.service.d', '/etc/systemd/user/service.d'):
            self.exists = {path}
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.snapshot()
        self.exists = set()
        self.links = {'/home/desktop/.local/state/omarchy-j71b-microphone/ownership.json'}
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_hung_failed_or_populated_service_refuses(self):
        for key, value in [('ActiveState', 'deactivating'), ('SubState', 'stop-sigterm'),
                           ('Result', 'timeout'), ('MainPID', '123'), ('ControlPID', '456'),
                           ('ExecMainStatus', '1'), ('DropInPaths', '/override')]:
            old = self.state[key]
            self.state[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'active, hung, failed'):
                self.snapshot()
            self.state[key] = old

    def test_removal_needs_clean_loaded_unit_and_absent_nodes(self):
        self.operation = 'remove'
        with self.assertRaisesRegex(ValueError, 'load state'):
            self.snapshot()
        self.state.update(LoadState='loaded', FragmentPath='/' + pkg.UNIT, ExecMainCode='1')
        self.exists = {'/' + pkg.UNIT}
        self.assertEqual(self.snapshot()['user'], 'desktop')
        self.graph[1]['info']['state'] = 'running'
        with self.assertRaisesRegex(ValueError, 'not healthy and idle'):
            self.snapshot()

    def test_user_manager_drift_and_faulted_journal_refuse(self):
        self.manager_version = '262.10-1'
        with self.assertRaisesRegex(ValueError, 'Running user manager'):
            self.snapshot()
        self.manager_version = '262.1-1'
        self.journal = 'J71b microphone stopped: source disappeared\n'
        with self.assertRaisesRegex(ValueError, 'service fault evidence'):
            self.snapshot()
        self.journal = ''
        self.manager_environment = 'PIPEWIRE_REMOTE=alternate\n'
        with self.assertRaisesRegex(ValueError, 'overrides'):
            self.snapshot()

    def test_command_timeout_propagates_without_retry_or_mutation(self):
        self.checker.user_command.side_effect = pkg.subprocess.TimeoutExpired('readonly', 8)
        with self.assertRaises(pkg.subprocess.TimeoutExpired):
            self.snapshot()
        self.assertEqual(self.checker.user_command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
