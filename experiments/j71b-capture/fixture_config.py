"""Print a disabled, synthetic-target-only PipeWire fixture configuration."""
import json
from pathlib import Path
import sys


def fixture(plugin):
    return {'context.modules': [{
        'name': 'libpipewire-module-filter-chain',
        'args': {
            'node.description': 'J71b capture experiment (synthetic fixture only)',
            'audio.channels': 2,
            'audio.position': ['FL', 'FR'],
            'audio.rate': 48000,
            'filter.graph': {
                'nodes': [{
                    'type': 'ladspa', 'name': 'capture', 'plugin': str(plugin),
                    'label': 'j71b_capture_experiment', 'control': {'Gain (dB)': 0},
                }],
                'inputs': ['capture:Input L', 'capture:Input R'],
                'outputs': ['capture:Output L', 'capture:Output R'],
            },
            'capture.props': {
                'node.name': 'capture.j71b.test.conditioned',
                'target.object': 'j71b.test.fixture',
                'node.autoconnect': False,
                'node.dont-fallback': True,
                'node.passive': True,
            },
            'playback.props': {
                'node.name': 'j71b.test.conditioned',
                'media.class': 'Audio/Source',
                'node.autoconnect': False,
            },
        },
    }]}

if __name__ == '__main__':
    plugin = Path(sys.argv[1] if len(sys.argv) == 2 else 'build/j71b-capture.so').resolve()
    if not plugin.is_file():
        raise SystemExit('Build the plugin first; an existing local .so is required')
    print(json.dumps(fixture(plugin), indent=2))
