"""Production microphone policy tests; no daemon, service or device is started."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
spec = importlib.util.spec_from_file_location('desktop_mic', RUNTIME)
mic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mic)


def node(ident, name, kind='Audio/Source', state='idle'):
    return {'id': ident, 'type': 'PipeWire:Interface:Node', 'info': {
        'state': state, 'props': {'node.name': name, 'object.serial': ident + 100,
                                'media.class': kind, 'audio.channels': 2}}}


def base():
    raw = node(10, mic.SOURCE)
    raw['info']['props'].update({'device.api': 'alsa', 'alsa.card_name': 'J71b Microphones',
                                'api.alsa.pcm.stream': 'capture'})
    return [raw, {'type': 'PipeWire:Interface:Core', 'info': {'version': '1.6.10'}}]


def link(ident, src, dst, channel=0):
    return {'id': ident, 'type': 'PipeWire:Interface:Link', 'info': {
        'output-node-id': src, 'input-node-id': dst, 'state': 'paused',
        'output-port-id': src * 10 + channel, 'input-port-id': dst * 10 + channel}}


def graph():
    data = base() + [node(20, mic.CAPTURE, 'Stream/Input/Audio'), node(21, mic.OUTPUT),
                     link(30, 10, 20), link(31, 10, 20, 1)]
    for ident in (10, 20, 21):
        for ch, name in enumerate(('FL', 'FR')):
            data.append({'id': ident * 10 + ch, 'type': 'PipeWire:Interface:Port',
                         'info': {'props': {'audio.channel': name}}})
    return data


class DesktopPolicy(unittest.TestCase):
    def test_exact_board_and_architecture_only(self):
        self.assertTrue(mic.supported_board(['apple,j71b', 'apple,t8010'], 'aarch64'))
        for compat, machine in [(['apple,j72b', 'apple,t8010'], 'aarch64'),
                                (['apple,j71b'], 'aarch64'),
                                (['apple,j71b', 'apple,t8010'], 'x86_64')]:
            self.assertFalse(mic.supported_board(compat, machine))

    def test_raw_source_requires_all_hardware_identity_fields(self):
        self.assertEqual(mic.serial(mic.raw_source(base())), '110')
        for key in ('device.api', 'alsa.card_name', 'api.alsa.pcm.stream', 'audio.channels', 'media.class'):
            data = base(); data[0]['info']['props'][key] = 'wrong'
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                mic.raw_source(data)
        for data in ([], base() + [deepcopy(base()[0])]):
            with self.assertRaises(RuntimeError):
                mic.raw_source(data)

    def test_numeric_serial_required(self):
        for value in ('', 'name-only', None, -1):
            data = base(); data[0]['info']['props']['object.serial'] = value
            with self.assertRaises(RuntimeError):
                mic.raw_source(data)

    def test_pinned_runtime_required(self):
        mic.check_version(base(), 'Compiled with libpipewire 1.6.10\nLinked with libpipewire 1.6.10\n')
        for text in ('Linked with libpipewire 1.6.100', 'Compiled with libpipewire 1.6.10'):
            with self.assertRaises(RuntimeError):
                mic.check_version(base(), text)
        data = base(); data[1]['info']['version'] = '1.4.2'
        with self.assertRaises(RuntimeError):
            mic.check_version(data, 'Linked with libpipewire 1.6.10')

    def test_config_is_passive_client_only_and_valid_priority(self):
        conf = mic.filter_config(Path('/installed/j71b-capture.so'), '110')
        self.assertIs(conf['context.properties']['core.daemon'], False)
        self.assertNotIn('context.objects', conf)
        args = conf['context.modules'][-1]['args']
        capture, output = args['capture.props'], args['playback.props']
        self.assertEqual(capture['target.object'], '110')
        for key in ('node.dont-fallback', 'node.dont-reconnect', 'node.passive', 'stream.dont-remix'):
            self.assertIs(capture[key], True)
        for key in ('node.always-process',):
            self.assertIs(capture[key], False)
            self.assertIs(output[key], False)
        self.assertIs(output['node.autoconnect'], False)
        self.assertEqual(output['priority.driver'], 0)
        self.assertEqual(output['priority.session'], 0)
        self.assertEqual(args['filter.graph']['nodes'][0]['control']['Gain (dB)'], 12)
        with self.assertRaises(ValueError):
            mic.filter_config(Path('/plugin'), 'name')
        with self.assertRaises(ValueError):
            mic.filter_config(Path('/plugin'), '110', 99)

    def test_arbitrary_consumers_and_user_defaults_are_allowed(self):
        data = graph() + [node(40, 'normal-mono-app', 'Stream/Input/Audio'),
                          node(41, 'second-app', 'Stream/Input/Audio'),
                          link(50, 21, 40), link(51, 21, 41), link(52, 21, 41, 1),
                          link(53, 10, 42)]
        data += [{'type': 'PipeWire:Interface:Metadata', 'metadata': [
            {'key': 'default.audio.source', 'value': {'name': mic.OUTPUT}}]}]
        raw, output, clients, other = mic.validate_graph(data, '110')
        self.assertEqual((clients, other), (3, 1))

    def test_replacement_error_and_wrong_link_are_fatal(self):
        cases = []
        data = graph(); data[0]['info']['props']['object.serial'] = 999; cases.append(data)
        data = graph(); data[0]['info']['state'] = 'error'; cases.append(data)
        data = graph(); data[2]['info']['error'] = 'XRUN'; cases.append(data)
        data = graph(); data[4]['info']['state'] = 'error'; cases.append(data)
        data = graph(); data[4]['info']['output-node-id'] = 90; cases.append(data)
        data = graph(); data[4]['info']['input-port-id'] = 201; cases.append(data)
        cases.append(graph() + [link(60, 20, 90)])
        cases.append(graph() + [link(60, 90, 21)])
        cases.append(graph() + [link(60, 10, 20)])
        for i, data in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(RuntimeError):
                mic.validate_graph(data, '110')

    def test_partial_initial_links_wait_but_later_loss_fails(self):
        data = [obj for obj in graph() if obj.get('id') != 31]
        with self.assertRaises(mic.PendingGraph):
            mic.validate_graph(data, '110', require_links=False)
        with self.assertRaises(RuntimeError) as raised:
            mic.validate_graph(data, '110', require_links=True)
        self.assertNotIsInstance(raised.exception, mic.PendingGraph)

    def test_isolation_rejects_loader_and_target_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for key in ('PIPEWIRE_CONFIG_NAME', 'PIPEWIRE_DAEMON', 'PIPEWIRE_PROPS',
                        'PIPEWIRE_MODULE_DIR', 'SPA_PLUGIN_DIR', 'LD_PRELOAD', 'LD_LIBRARY_PATH'):
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    mic.private_environment({key: 'override'}, directory)
            with self.assertRaises(RuntimeError):
                mic.private_environment({'PIPEWIRE_REMOTE': 'private-server'}, directory)
            env = mic.private_environment({'HOME': '/existing/home'}, directory)
            self.assertEqual(env['PIPEWIRE_CONFIG_PREFIX'], str(directory))
            conf = json.loads((directory / 'client.conf').read_text())
            self.assertEqual(len(conf['context.modules']), 3)
            self.assertNotIn('stream.rules', conf)

    def test_guard_blocks_duplicate_and_survives_session_recreation(self):
        with tempfile.TemporaryDirectory() as temp:
            with mic.OwnershipGuard(Path(temp), 'boot-one') as guard:
                guard.check()
                with self.assertRaises(RuntimeError):
                    mic.OwnershipGuard(Path(temp), 'boot-one')
                guard.mark()
            with mic.OwnershipGuard(Path(temp), 'boot-one') as guard:
                with self.assertRaises(RuntimeError): guard.check()
                with self.assertRaises(RuntimeError): guard.reset_after_physical_reset()
            with mic.OwnershipGuard(Path(temp), 'boot-two') as guard:
                # Changed boot alone never silently reopens the driver.
                with self.assertRaises(RuntimeError): guard.check()
                guard.reset_after_physical_reset()
                guard.check()

    def test_clean_guard_can_start_again(self):
        with tempfile.TemporaryDirectory() as temp:
            with mic.OwnershipGuard(Path(temp), 'boot') as guard:
                guard.mark(); guard.clean(); guard.check()

    def test_guard_syncs_new_ancestry_marker_and_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'new-state' / 'microphone'
            with patch.object(mic, 'sync_directory', wraps=mic.sync_directory) as sync:
                with mic.OwnershipGuard(directory, 'boot') as guard:
                    self.assertIn(Path(temp), [call.args[0] for call in sync.call_args_list])
                    sync.reset_mock()
                    guard.mark()
                    sync.assert_called_once_with(directory)
                    sync.reset_mock()
                    guard.clean()
                    sync.assert_called_once_with(directory)

    def test_guard_rejects_symlink_directory_and_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'real').mkdir(); (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(RuntimeError): mic.OwnershipGuard(root / 'link', 'boot')
            with mic.OwnershipGuard(root / 'real', 'boot') as guard:
                guard.marker.symlink_to(root / 'missing')
                with self.assertRaises(RuntimeError): guard.check()
                with self.assertRaises(RuntimeError): guard.reset_after_physical_reset()

    def test_hung_filter_is_not_force_killed_or_restarted(self):
        child = Mock(); child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired('filter', 5)
        with self.assertRaises(RuntimeError): mic.stop_filter(child)
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.kill.assert_not_called(); child.terminate.assert_not_called()

    def test_cleanup_requires_removed_nodes_same_source_and_idle(self):
        self.assertTrue(mic.cleanup_verified(base(), '110'))
        self.assertFalse(mic.cleanup_verified(graph(), '110'))
        data = base(); data[0]['info']['state'] = 'running'
        self.assertFalse(mic.cleanup_verified(data, '110'))
        with self.assertRaises(RuntimeError): mic.cleanup_verified(base(), '999')

    def test_service_has_no_autostart_or_restart(self):
        unit = (PROJECT / 'system/overlay/usr/lib/systemd/user/omarchy-j71b-microphone.service').read_text()
        self.assertIn('Restart=no', unit)
        self.assertIn('Requisite=pipewire.service wireplumber.service', unit)
        self.assertNotIn('Requires=', unit)
        self.assertIn('SendSIGKILL=no', unit)
        self.assertIn('ConditionArchitecture=arm64', unit)
        self.assertNotIn('\n[Install]', unit)
        self.assertFalse(list((PROJECT / 'system/overlay').glob('**/*.wants/omarchy-j71b-microphone.service')))


class SupervisorPolicy(unittest.TestCase):
    def setUp(self):
        mic.STOP = False

    def tearDown(self):
        mic.STOP = False

    def run_service(self, snapshots, child=None, sleep=None):
        child = child or Mock()
        child.poll.return_value = None
        child.wait.return_value = 0
        guard = Mock()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(mic, 'snapshot', side_effect=snapshots), \
                patch.object(mic, 'command', return_value='Linked with libpipewire 1.6.10'), \
                patch.object(mic.subprocess, 'Popen', return_value=child) as popen, \
                patch.object(mic.time, 'sleep', side_effect=sleep):
            mic.serve({}, Path(temp), Path('/plugin.so'), guard)
        return child, guard, popen

    def test_normal_stop_cleans_only_after_idle(self):
        def stop(unused): mic.STOP = True
        child, guard, popen = self.run_service([base(), graph(), base()], sleep=stop)
        guard.mark.assert_called_once(); guard.clean.assert_called_once()
        child.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(popen.call_count, 1)

    def test_graph_fault_retains_marker_without_reopen(self):
        bad = graph(); bad[0]['info']['props']['object.serial'] = 999
        guard, child = Mock(), Mock(); child.poll.return_value = None; child.wait.return_value = 0
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(mic, 'snapshot', side_effect=[base(), bad]), \
                patch.object(mic, 'command', return_value='Linked with libpipewire 1.6.10'), \
                patch.object(mic.subprocess, 'Popen', return_value=child) as popen:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                mic.serve({}, Path(temp), Path('/plugin'), guard)
        guard.mark.assert_called_once(); guard.clean.assert_not_called()
        self.assertEqual(popen.call_count, 1)

    def test_server_disconnect_retains_marker(self):
        guard, child = Mock(), Mock(); child.poll.return_value = None; child.wait.return_value = 0
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(mic, 'snapshot', side_effect=[base(), RuntimeError('server disconnected')]), \
                patch.object(mic, 'command', return_value='Linked with libpipewire 1.6.10'), \
                patch.object(mic.subprocess, 'Popen', return_value=child) as popen:
            with self.assertRaisesRegex(RuntimeError, 'server disconnected'):
                mic.serve({}, Path(temp), Path('/plugin'), guard)
        guard.clean.assert_not_called(); self.assertEqual(popen.call_count, 1)


    def test_unexpected_exit_coincident_with_stop_retains_marker(self):
        guard, child = Mock(), Mock()
        child.poll.side_effect = [None, 1]
        def stop(unused): mic.STOP = True
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(mic, 'snapshot', side_effect=[base(), graph()]), \
                patch.object(mic, 'command', return_value='Linked with libpipewire 1.6.10'), \
                patch.object(mic.subprocess, 'Popen', return_value=child), \
                patch.object(mic.time, 'sleep', side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, 'before requested shutdown'):
                mic.serve({}, Path(temp), Path('/plugin'), guard)
        guard.clean.assert_not_called(); child.send_signal.assert_not_called()

    def test_preexisting_raw_consumer_prevents_start_without_marker(self):
        data = base(); data[0]['info']['state'] = 'running'
        guard = Mock()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(mic, 'snapshot', return_value=data), \
                patch.object(mic, 'command', return_value='Linked with libpipewire 1.6.10'), \
                patch.object(mic.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'Close recording apps'):
                mic.serve({}, Path(temp), Path('/plugin'), guard)
        guard.mark.assert_not_called(); popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
