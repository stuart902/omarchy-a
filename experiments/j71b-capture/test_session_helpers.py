"""Pure helper checks for the CI session harness; never launch a process."""
import unittest
from types import SimpleNamespace

from test_desktop_session import FALLBACK, client_config, consumer_routes_ready, default_source, props, server_config


class SessionHelpers(unittest.TestCase):
    def test_only_synthetic_sources_use_follower_clock(self):
        config = server_config(SimpleNamespace(SOURCE='synthetic-raw'))
        self.assertEqual(config['node.rules'], [{
            'matches': [{'node.name': 'synthetic-raw'}, {'node.name': FALLBACK}],
            'actions': {'update-props': {'node.driver': False}}}])
        self.assertNotIn('always-process', str(config))

    def test_consumer_routes_follow_actual_ports_not_pcm_channel_count(self):
        data = [
            {'id': i, 'type': 'PipeWire:Interface:Node',
             'info': {'state': 'running', 'props': {'node.name': name}}}
            for i, name in ((1, 'source'), (2, 'mono-consumer'))]
        for i, channel in ((10, 'FL'), (11, 'FR')):
            data.append({'id': i, 'type': 'PipeWire:Interface:Port', 'info': {
                'direction': 'input', 'props': {'node.id': 2, 'audio.channel': channel}}})
            data.append({'type': 'PipeWire:Interface:Link', 'info': {
                'output-node-id': 1, 'input-node-id': 2, 'input-port-id': i, 'state': 'active'}})
        self.assertTrue(consumer_routes_ready(data, {'mono-consumer': 'source'}, {}))
        missing = data[:-1]
        self.assertFalse(consumer_routes_ready(missing, {'mono-consumer': 'source'}, {}))
        data[-1]['info']['output-node-id'] = 99
        with self.assertRaisesRegex(AssertionError, 'wrong source'):
            consumer_routes_ready(data, {'mono-consumer': 'source'}, {})

    def test_client_can_bind_metadata(self):
        config = client_config({'context.spa-libs': {}})
        self.assertIn({'name': 'libpipewire-module-metadata'},
                      config['context.modules'])

    def test_metadata_uses_top_level_properties(self):
        metadata = {'type': 'PipeWire:Interface:Metadata',
                    'props': {'metadata.name': 'default'},
                    'metadata': [{'key': 'default.audio.source',
                                  'value': {'name': FALLBACK}}]}
        self.assertEqual(default_source([metadata]), FALLBACK)
        metadata['metadata'][0]['value'] = '{"name":"' + FALLBACK + '"}'
        self.assertEqual(default_source([metadata]), FALLBACK)

    def test_node_uses_info_properties(self):
        self.assertEqual(props({'info': {'props': {'node.name': 'test'}}}),
                         {'node.name': 'test'})


if __name__ == '__main__':
    unittest.main()
