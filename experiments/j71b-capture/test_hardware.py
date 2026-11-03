"""Hardware launcher policy tests. No daemon, device, or subprocess is started."""
import signal
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import wave
from pathlib import Path
from hardware_test import (CAPTURE, OUTPUT, RECORDER, SOURCE, check_kernel,
                           check_routes, check_versions, clean_environment, config,
                           defaults, isolate_client_environment, preflight,
                           recording_stats, stop_child, validate_recorder_completion)


def n(ident, name, kind='Audio/Source', state='idle'):
    return {'id': ident, 'type': 'PipeWire:Interface:Node', 'info': {
        'state': state, 'props': {'node.name': name, 'object.serial': ident + 100,
                                'media.class': kind, 'audio.channels': 2}}}


def link(ident, src, dst):
    return {'id': ident, 'type': 'PipeWire:Interface:Link',
            'info': {'output-node-id': src, 'input-node-id': dst,
                     'output-port-id': src * 10 + ident % 2,
                     'input-port-id': dst * 10 + ident % 2}}


def ports(ident):
    return [{'id': ident * 10 + i, 'type': 'PipeWire:Interface:Port',
             'info': {'props': {'node.id': ident, 'audio.channel': channel}}}
            for i, channel in enumerate(('FL', 'FR'))]


def fixture():
    return [n(10, SOURCE), {'type': 'PipeWire:Interface:Metadata', 'metadata': [
        {'key': 'default.audio.source', 'subject': 0, 'value': {'name': SOURCE}}]}]


def routed():
    return fixture() + [n(20, CAPTURE, 'Stream/Input/Audio'), n(21, OUTPUT),
                        link(30, 10, 20), link(31, 10, 20)] + ports(10) + ports(20) + ports(21)


class HardwarePolicy(unittest.TestCase):
    def test_preflight_accepts_exact_idle_j71b(self):
        self.assertEqual(preflight(fixture(), ['apple,j71b', 'apple,t8010'], 'aarch64')['id'], 10)

    def test_wrong_board_or_architecture_rejected(self):
        for compat, machine in [(['apple,j71b'], 'aarch64'),
                                (['apple,j71b', 'apple,t8010'], 'x86_64'),
                                (['apple,j72b', 'apple,t8010'], 'aarch64')]:
            with self.assertRaises(RuntimeError):
                preflight(fixture(), compat, machine)

    def test_existing_capture_test_or_playback_rejected(self):
        cases = []
        busy = fixture(); busy[0]['info']['state'] = 'running'; cases.append(busy)
        cases.append(fixture() + [n(20, OUTPUT)])
        cases.append(fixture() + [n(20, 'alsa_output.platform-speakers', 'Audio/Sink')])
        cases.append(fixture() + [n(20, SOURCE)])
        missing_serial = fixture(); del missing_serial[0]['info']['props']['object.serial']; cases.append(missing_serial)
        mono = fixture(); mono[0]['info']['props']['audio.channels'] = 1; cases.append(mono)
        for data in cases:
            with self.assertRaises(RuntimeError):
                preflight(data, ['apple,j71b', 'apple,t8010'], 'aarch64')

    def test_config_is_client_and_capture_only(self):
        data = config(Path('/package/j71b-capture.so'), '110', 12)
        self.assertIs(data['context.properties']['core.daemon'], False)
        self.assertNotIn('context.objects', data)
        args = data['context.modules'][-1]['args']
        capture, output = args['capture.props'], args['playback.props']
        self.assertEqual(capture['target.object'], '110')
        for prop in ['node.dont-fallback', 'node.dont-reconnect', 'node.passive']:
            self.assertIs(capture[prop], True)
        self.assertIs(output['node.autoconnect'], False)
        self.assertEqual(output['media.class'], 'Audio/Source')
        self.assertLess(output['priority.session'], 0)
        self.assertEqual(args['filter.graph']['nodes'][0]['control']['Gain (dB)'], 12)

    def test_exact_routes_accepted(self):
        data = routed()
        self.assertEqual(check_routes(data, '110', defaults(fixture()))[2], 0)
        data += [n(40, RECORDER, 'Stream/Input/Audio'), link(41, 21, 40), link(42, 21, 40)] + ports(40)
        self.assertEqual(check_routes(data, '110', defaults(fixture()), True)[2], 2)
        data = [o for o in data if o.get('id') != 40]
        self.assertEqual(check_routes(data, '110', defaults(fixture()), True, 40)[2], 2)
        with self.assertRaises(RuntimeError):
            check_routes(data, '110', defaults(fixture()), True, 99)

    def test_unknown_route_or_default_change_rejected(self):
        cases = []
        fallback = routed(); fallback[4]['info']['output-node-id'] = 99; cases.append(fallback)
        cases.append(routed() + [link(32, 21, 99)])
        cases.append(routed() + [n(40, RECORDER), link(41, 10, 40)])
        changed = routed(); changed[0]['info']['props']['object.serial'] = 999; cases.append(changed)
        changed = routed(); changed[1]['metadata'][0]['value']['name'] = OUTPUT; cases.append(changed)
        cases.append(routed() + [link(33, 20, 99)])
        cases.append(routed() + [link(33, 99, 21)])
        cases.append(routed() + [link(33, 10, 99)])
        cases.append([o for o in routed() if o.get('id') != 201])
        crossed = routed(); crossed[4]['info']['input-port-id'] = 201; cases.append(crossed)
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(RuntimeError):
                check_routes(data, '110', defaults(fixture()), True)

    def test_environment_cannot_override_client_or_target(self):
        for key in ['PIPEWIRE_DAEMON', 'PIPEWIRE_CONFIG_NAME', 'PIPEWIRE_CONFIG_PREFIX',
                    'PIPEWIRE_PROPS', 'PIPEWIRE_NODE', 'PIPEWIRE_AUTOCONNECT',
                    'PIPEWIRE_MODULE_DIR', 'SPA_PLUGIN_DIR', 'LD_PRELOAD', 'LD_LIBRARY_PATH']:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                clean_environment({key: 'override'})
        with self.assertRaises(RuntimeError):
            clean_environment({'PIPEWIRE_REMOTE': 'other-server'})
        cleaned = clean_environment({'XDG_RUNTIME_DIR': '/run/user/1000', 'PIPEWIRE_REMOTE': 'pipewire-0'})
        self.assertEqual(cleaned['PIPEWIRE_DAEMON'], 'false')

    def test_kernel_fault_markers_rejected(self):
        for message in ['', '-- No entries --\n', 'boot_id=abc\n',
                        'initial PDM clock OFF failed (-90); retaining resources until device reset',
                        'DMA termination failed', 'apple-sio: timed out', 'AOP transport fault',
                        'BUG: sleeping function called', 'Oops: kernel fault']:
            with self.assertRaises(RuntimeError):
                check_kernel(message)
        check_kernel('t8010-pdm: registered capture PCM\nLinux version 6.18')

    def test_exact_client_and_server_version_required(self):
        data = [{'type': 'PipeWire:Interface:Core', 'info': {'version': '1.6.10'}}]
        check_versions(data, 'Compiled with libpipewire 1.6.10\nLinked with libpipewire 1.6.10\n')
        for text in ['Compiled with libpipewire 1.6.10\nLinked with libpipewire 1.4.2\n',
                     'Linked with libpipewire 1.6.100\n']:
            with self.assertRaises(RuntimeError):
                check_versions(data, text)
        data[0]['info']['version'] = '1.4.2'
        with self.assertRaises(RuntimeError):
            check_versions(data, 'Linked with libpipewire 1.6.10\n')

    def test_all_clients_use_private_config_without_stream_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            env = isolate_client_environment({'HOME': '/existing/home'}, Path(directory))
            self.assertEqual(env['PIPEWIRE_CONFIG_PREFIX'], str(Path(directory).resolve()))
            self.assertEqual(env['PIPEWIRE_CONFIG_NAME'], 'client.conf')
            self.assertEqual(env['PIPEWIRE_DAEMON'], 'false')
            data = json.loads((Path(directory)/'client.conf').read_text())
            self.assertNotIn('stream.rules', data)
            self.assertIs(data['context.properties']['core.daemon'], False)
            self.assertEqual([x['name'] for x in data['context.modules']],
                             ['libpipewire-module-protocol-native',
                              'libpipewire-module-client-node', 'libpipewire-module-adapter'])

    def test_short_or_wrong_format_wav_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.wav'
            for rate, frames in [(44100, 480000), (48000, 100), (48000, 480000)]:
                with wave.open(str(path), 'wb') as wav:
                    wav.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
                    wav.writeframes(b'\0' * frames * 4)
                if rate == 48000 and frames == 480000:
                    self.assertEqual(recording_stats(path)['frames'], 480000)
                else:
                    with self.assertRaises(RuntimeError):
                        recording_stats(path)
            with path.open('r+b') as truncated:
                truncated.truncate(1024)
            with self.assertRaisesRegex(RuntimeError, 'payload is truncated'):
                recording_stats(path)

    def test_cleanup_sends_one_interrupt_and_never_force_kills(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired('fixture', 5)
        with self.assertRaises(RuntimeError):
            stop_child(child, 'Fixture')
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_sample_limit_status_requires_complete_wav_and_observed_route(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0' * 480000 * 4)
            for code in (0, 1):
                self.assertEqual(validate_recorder_completion(path, code, True, '')['frames'], 480000)
                with self.assertRaisesRegex(RuntimeError, 'route was not observed'):
                    validate_recorder_completion(path, code, False, '')
                with self.assertRaisesRegex(RuntimeError, 'emitted diagnostics'):
                    validate_recorder_completion(path, code, True, 'stream error\n')
            for code in (-signal.SIGINT, -signal.SIGSEGV, 2, 127, None):
                with self.subTest(returncode=code), self.assertRaisesRegex(RuntimeError, 'exited with status'):
                    validate_recorder_completion(path, code, True, '')
            with path.open('r+b') as truncated:
                truncated.truncate(1024)
            with self.assertRaisesRegex(RuntimeError, 'payload is truncated'):
                validate_recorder_completion(path, 1, True, '')

    def test_sample_limit_status_cannot_accept_short_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'short.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0' * 100 * 4)
            with self.assertRaisesRegex(RuntimeError, 'expected ten seconds'):
                validate_recorder_completion(path, 1, True, '')


if __name__ == '__main__':
    unittest.main()
