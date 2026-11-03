"""Run the real installed PipeWire SPA engine without a daemon or devices."""
import json
import os
from pathlib import Path
import unittest

import numpy as np
from spa_graph import Graph, stock_graph

PLUGIN = Path(os.environ.get('J71B_TEST_PLUGIN', './build/j71b-capture.so')).resolve()
RATE = 48000

def candidate(gain=0):
    return {
        'nodes': [{'type': 'ladspa', 'name': 'capture', 'plugin': str(PLUGIN),
                   'label': 'j71b_capture_experiment', 'control': {'Gain (dB)': gain}}],
        'inputs': ['capture:Input L', 'capture:Input R'],
        'outputs': ['capture:Output L', 'capture:Output R'],
    }

def render(x, gain=0, blocksize=256, rate=RATE):
    with Graph(candidate(gain), rate=rate) as graph:
        return graph.process(x, blocksize)

class ActualGraph(unittest.TestCase):
    def test_constant_offset_has_no_opening_step(self):
        for level in [-.5, -.06, .06, .5]:
            x = np.full((RATE, 2), level, np.float32)
            y = render(x, gain=24)
            self.assertLess(float(np.max(abs(y))), 1e-8)

    def test_silence_stays_silent(self):
        np.testing.assert_array_equal(render(np.zeros((RATE, 2))), 0)

    def test_initial_zero_prefill_does_not_use_initialization(self):
        x = np.full((RATE, 2), -.06, np.float32)
        x[:5317] = 0  # intentionally not aligned to the graph quantum
        self.assertLess(float(np.max(abs(render(x, gain=24)))), 1e-8)

    def test_reset_reinitializes_offset_and_ramp(self):
        x = np.full((RATE, 2), -.06, np.float32)
        with Graph(candidate(24)) as graph:
            graph.process(x)
            graph.reset()
            y = graph.process(-x)
        self.assertLess(float(np.max(abs(y))), 1e-8)

    def test_deactivate_reactivate_reinitializes(self):
        x = np.full((RATE, 2), -.06, np.float32)
        with Graph(candidate(24)) as graph:
            graph.process(x)
            graph.deactivate()
            graph.activate()
            y = graph.process(-x)
        self.assertLess(float(np.max(abs(y))), 1e-8)

    def test_ac_onset_has_the_20ms_fade_envelope(self):
        t = np.arange(RATE//10) / RATE
        x = np.column_stack((-.06+.01*np.sin(2*np.pi*1000*t), -.05*np.ones(len(t))))
        # Settle a separate stock filter at the input's initial DC value.
        # Its AC response supplies an independent reference without our fade.
        with Graph(stock_graph()) as stock:
            stock.process(np.tile(x[0], (RATE, 1)))
            unfaded = stock.process(x)
        envelope = np.minimum(np.arange(len(t)) / (RATE*.020), 1.0)
        expected = unfaded * envelope[:, None]
        actual = render(x)
        np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=0)
        self.assertGreater(float(np.max(abs(unfaded[:480, 0]-actual[:480, 0]))), .005)

    def test_ac_reset_restarts_fade_and_gain_smoothing(self):
        t = np.arange(RATE//10) / RATE
        x = np.column_stack((-.06+.01*np.sin(2*np.pi*1000*t),
                             -.05+.02*np.sin(2*np.pi*730*t)))
        fresh = render(x, 24)
        with Graph(candidate(24)) as graph:
            graph.process(np.tile(x, (10, 1)))
            graph.reset()
            reset = graph.process(x)
            graph.deactivate()
            graph.activate()
            recreated = graph.process(x)
        np.testing.assert_array_equal(reset, fresh)
        np.testing.assert_array_equal(recreated, fresh)

    def test_block_partition_independence(self):
        rng = np.random.default_rng(4)
        x = rng.normal(-.06, .005, (18000, 2)).astype(np.float32)
        reference = render(x, 18, 1024)
        for block in [1, 127, 256, 511]:
            np.testing.assert_array_equal(render(x, 18, block), reference)

    def test_voiceband_gain_and_channel_isolation(self):
        t = np.arange(RATE) / RATE
        x = np.column_stack((-.06 + .005*np.sin(2*np.pi*1000*t), -.05*np.ones(RATE)))
        for gain in [0, 12, 24]:
            y = render(x, gain)[RATE//2:]
            expected = .005 * 10**(gain/20) / np.sqrt(2)
            self.assertAlmostEqual(float(np.sqrt(np.mean(y[:, 0]**2)))/expected, 1, places=3)
            self.assertLess(float(np.max(abs(y[:, 1]))), 1e-8)

    def test_80hz_cutoff(self):
        t = np.arange(RATE) / RATE
        x = np.column_stack((-.06+.005*np.sin(2*np.pi*80*t), np.zeros(RATE)))
        y = render(x)[RATE//2:, 0]
        ratio = np.sqrt(np.mean(y*y)) / (.005 / np.sqrt(2))
        self.assertAlmostEqual(20*np.log10(ratio), -3.0103, places=2)

    def test_overload_has_explicit_digital_bound(self):
        x = np.tile(np.array([[-1., 1.], [1., -1.]], dtype=np.float32), (RATE//2, 1))
        y = render(x, 24)
        self.assertTrue(np.all(np.isfinite(y)))
        self.assertLessEqual(float(np.max(abs(y))), .891251)
        self.assertGreater(float(np.max(abs(y))), .89)

    def test_nonfinite_samples_do_not_poison_channel(self):
        t = np.arange(RATE)/RATE
        x = np.column_stack((-.06+.001*np.sin(2*np.pi*1000*t), -.05*np.ones(RATE)))
        x[2000:2003, 0] = [np.nan, np.inf, -np.inf]
        y = render(x)
        self.assertTrue(np.all(np.isfinite(y)))
        np.testing.assert_array_equal(y[2000:2003, 0], 0)
        self.assertGreater(float(np.std(y[-1000:, 0])), .0006)
        self.assertLess(float(np.max(abs(y[:, 1]))), 1e-8)

    def test_supported_rate_range(self):
        for rate in [8000, 44100, 48000, 96000, 192000]:
            x = np.full((rate//10, 2), .06, np.float32)
            self.assertLess(float(np.max(abs(render(x, 24, rate=rate)))), 1e-8)

    def test_stock_highpass_regression_fixture_exposes_start_step(self):
        x = np.full((RATE, 2), .13, np.float32)
        with Graph(stock_graph()) as graph:
            y = graph.process(x)
        self.assertGreater(float(abs(y[0, 0])), .12)
        self.assertLess(float(abs(y[-1, 0])), 1e-5)

    def test_stock_ramp_does_not_restart_on_graph_reset(self):
        graph = stock_graph()
        graph['nodes'] += [
            {'type': 'builtin', 'name': 'ramp', 'label': 'ramp',
             'control': {'Start': 0, 'Stop': 1, 'Duration (s)': .02}},
        ] + [{'type': 'builtin', 'name': f'm{i}', 'label': 'mult'} for i in range(2)]
        graph['links'] = [
            {'output': f'hp{i}:Out', 'input': f'm{i}:In 1'} for i in range(2)
        ] + [{'output': 'ramp:Out', 'input': f'm{i}:In 2'} for i in range(2)]
        graph['outputs'] = ['m0:Out', 'm1:Out']
        x = np.full((RATE, 2), .13, np.float32)
        with Graph(graph) as engine:
            first = engine.process(x)
            engine.reset()
            resumed = engine.process(x)
        self.assertEqual(float(first[0, 0]), 0)
        self.assertGreater(float(resumed[0, 0]), .12)

    def test_fixture_is_inert_and_synthetic_only(self):
        from fixture_config import fixture
        config = fixture(PLUGIN)
        args = config['context.modules'][0]['args']
        self.assertEqual(args['capture.props']['target.object'], 'j71b.test.fixture')
        self.assertIs(args['capture.props']['node.autoconnect'], False)
        self.assertIs(args['playback.props']['node.autoconnect'], False)
        self.assertIs(args['capture.props']['node.dont-fallback'], True)
        self.assertEqual(args['filter.graph']['nodes'][0]['control']['Gain (dB)'], 0)
        self.assertNotIn('alsa_input', json.dumps(config))

    def test_midstream_offset_jump_is_not_silently_claimed_fixed(self):
        # No buffer discontinuity metadata reaches a LADSPA process callback.
        # A real unexpected DC jump still produces a transient; disclose this.
        x = np.full((RATE, 2), -.06, np.float32)
        x[RATE//2:] = .06
        y = render(x)
        self.assertGreater(float(abs(y[RATE//2, 0])), .1)
        self.assertLess(float(abs(y[-1, 0])), 1e-8)

if __name__ == '__main__':
    unittest.main()
