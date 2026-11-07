"""Pure helper checks for the CI session harness; never launch a process."""
import unittest

from test_desktop_session import FALLBACK, client_config, default_source, props


class SessionHelpers(unittest.TestCase):
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
