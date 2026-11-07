#!/usr/bin/env python3
"""Socket-free tests of the synthetic session's PulseAudio coverage helpers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np

import test_desktop_session as fixture


def tone(rate=48000, channels=2, conditioned=True):
    # Independently specified 1 kHz fixture with unequal stereo amplitudes.
    amplitudes = np.array([.01, .005]) if channels == 2 else np.array([.0075])
    if conditioned:
        amplitudes *= 10 ** .6
    phase = 2 * np.pi * 1000 * np.arange(rate * 3 // 4) / rate + .4
    return np.sin(phase)[:, None] * amplitudes


def node(ident, name):
    return {'id': ident, 'type': 'PipeWire:Interface:Node',
            'info': {'state': 'running', 'props': {'node.name': name}}}


def route_graph():
    data = [node(10, 'raw'), node(20, 'capture'), node(30, 'conditioned'),
            node(40, 'j71b.test.app'), node(50, fixture.FALLBACK)]
    for ident in (10, 20, 30, 40):
        for channel, label in enumerate(('FL', 'FR')):
            data.append({'id': ident * 10 + channel, 'type': 'PipeWire:Interface:Port',
                         'info': {'direction': 'input' if ident in (20, 40) else 'output',
                                  'props': {'node.id': ident, 'audio.channel': label}}})
    for ident, source, target in ((60, 10, 20), (70, 30, 40)):
        for channel in (0, 1):
            data.append({'id': ident + channel, 'type': 'PipeWire:Interface:Link',
                         'info': {'state': 'active', 'input-node-id': target,
                                  'output-node-id': source,
                                  'input-port-id': target * 10 + channel,
                                  'output-port-id': source * 10 + channel}})
    return data


class PulseFixtureTests(unittest.TestCase):
    def test_inherited_pulse_overrides_are_removed(self):
        self.assertEqual(fixture.clean_environment({
            'PULSE_SERVER': 'unix:/other-session', 'PULSE_COOKIE': '/private/cookie',
            'PULSE_CLIENTCONFIG': '/other/config', 'PULSE_RUNTIME_PATH': '/other',
            'PIPEWIRE_REMOTE': 'other', 'LD_PRELOAD': '/library', 'PATH': '/usr/bin'}),
            {'PATH': '/usr/bin'})

    def test_pulse_bridge_uses_only_private_socket_and_no_autoload(self):
        server = fixture.server_config(Mock(SOURCE='raw'))
        snapshot = deepcopy(server)
        config = fixture.pulse_config(server, Path('/private/pulse-native'))
        self.assertEqual(config['pulse.properties']['server.address'], ['unix:/private/pulse-native'])
        self.assertEqual(config['pulse.cmd'], [])
        self.assertFalse(config['context.properties']['core.daemon'])
        self.assertNotIn('context.objects', config)
        self.assertNotIn('context.exec', config)
        self.assertIn({'name': 'libpipewire-module-protocol-pulse'}, config['context.modules'])
        self.assertEqual(server, snapshot)

    def test_parec_named_default_and_pinned_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Mock(root=Path(directory))
            for target, pinned in (('conditioned', False), (None, False), ('conditioned', True)):
                fixture.pulse_recorder(session, 'app', target, 16000, 1, pinned)
                command = session.start.call_args.args[1]
                self.assertEqual(command[0], 'parec')
                for option in ('--raw', '--format=float32le', '--rate=16000',
                               '--channels=1', '--channel-map=mono',
                               '--property=node.name=j71b.test.app'):
                    self.assertIn(option, command)
                self.assertEqual('--device=conditioned' in command, target is not None)
                for key in ('dont-fallback', 'dont-reconnect'):
                    self.assertEqual(f'--property=node.{key}=true' in command, pinned)
            with self.assertRaises(AssertionError):
                fixture.pulse_recorder(session, 'invalid', None, pinned=True)

    def test_stereo_and_mono_pcm_match_fixed_gain_at_all_client_rates(self):
        for rate, channels in ((48000, 2), (48000, 1), (44100, 2), (16000, 1)):
            for conditioned in (False, True):
                with self.subTest(rate=rate, channels=channels, conditioned=conditioned):
                    result = fixture.check_tone(tone(rate, channels, conditioned), rate, channels, conditioned)
                    self.assertEqual(result['gain_db'], 12 if conditioned else 0)
                    self.assertEqual(len(result['rms']), channels)

    def test_expected_response_includes_high_pass(self):
        gain = fixture.tone_expectation(2, True) / np.array([.01, .005])
        np.testing.assert_allclose(gain, 3.980990647, rtol=1e-7)
        self.assertLess(gain[0], 10 ** .6)
        np.testing.assert_allclose(fixture.tone_expectation(1, True),
                                   [fixture.tone_expectation(2, True).mean()])

    def test_pcm_rejects_silence_wrong_gain_channels_and_frequency(self):
        good = tone()
        wrong_frequency = np.sin(2 * np.pi * 440 * np.arange(len(good)) / 48000)[:, None]
        wrong_frequency = wrong_frequency * np.array([.01, .005]) * 10 ** .6
        bad = {
            'silent': good * 0, 'raw-fallback': good / 10 ** .6,
            'extra-12db': good * 10 ** .6, 'swapped': good[:, ::-1],
            'missing-right': good * [1, 0], 'inverted-right': good * [1, -1],
            'wrong-frequency': wrong_frequency, 'dc': good + .003,
            'too-short': good[:1000], 'nan': good * np.nan,
        }
        dropped = good.copy()
        dropped[-4000:-3000] = 0
        bad['dropout'] = dropped
        for name, samples in bad.items():
            with self.subTest(name=name), self.assertRaises(AssertionError):
                fixture.check_tone(samples, 48000, 2, True)

    def test_pcm_rejects_incorrect_mono_mix(self):
        for factor in (2 / 3, 4 / 3, 2):
            with self.subTest(factor=factor), self.assertRaises(AssertionError):
                fixture.check_tone(tone(channels=1) * factor, 48000, 1, True)

    def test_parec_requires_clean_exit_unlike_pw_cat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recording.f32'
            tone().astype('<f4').tofile(path)
            path.with_suffix('.log').write_text('')
            session = Mock()
            session.stop.return_value = 1
            with self.assertRaisesRegex(AssertionError, 'Recorder failed'):
                fixture.finish_recording(session, Mock(), path, 48000, 2, accepted_codes=(0,))
            session.stop.return_value = 0
            self.assertEqual(fixture.finish_recording(session, Mock(), path, 48000, 2,
                                                      accepted_codes=(0,))['exit'], 0)

    def test_route_requires_matching_native_ports_and_pulse_source_index(self):
        graph = route_graph()
        session = Mock()
        session.runtime.SOURCE, session.runtime.CAPTURE = 'raw', 'capture'
        session.await_graph.side_effect = lambda predicate, _: self.assertTrue(predicate(graph))
        session.pulse_sources.return_value = [{'name': 'conditioned', 'index': 123}]
        stream = {'properties': {'node.name': 'j71b.test.app'}, 'source': 123}
        session.run.return_value = json.dumps([stream])
        fixture.pulse_route(session, 'app', 'conditioned', 2)
        stream['source'] = 124
        session.run.return_value = json.dumps([stream])
        with self.assertRaisesRegex(AssertionError, 'Wrong PulseAudio source index'):
            fixture.pulse_route(session, 'app', 'conditioned', 2)
        graph[-1]['info']['output-port-id'] = 300
        with self.assertRaisesRegex(AssertionError, 'Crossed PulseAudio stereo ports'):
            fixture.pulse_route(session, 'app', 'conditioned', 2)

    def test_no_fallback_rejects_raw_and_other_source_links(self):
        graph = route_graph()
        fixture.assert_no_consumer_fallback(graph, 'app', 'conditioned')
        for source_id in (10, 50):
            graph[-1]['info']['output-node-id'] = source_id
            with self.subTest(source=source_id), self.assertRaisesRegex(AssertionError, 'fell back'):
                fixture.assert_no_consumer_fallback(graph, 'app', 'conditioned')
        fixture.assert_no_consumer_fallback(graph[:-2], 'app')


if __name__ == '__main__':
    unittest.main(verbosity=2)
