"""Check config isolation with pw-config only; never load modules or connect."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hardware_test import config, isolate_client_environment


def main(tool):
    with tempfile.TemporaryDirectory(prefix='j71b-config-parser-') as directory:
        root = Path(directory)
        private = root/'private'
        private.mkdir()
        home = root/'home'
        override = home/'.config/pipewire/client.conf.d'
        override.mkdir(parents=True)
        (override/'99-poison.conf').write_text(
            'stream.rules = [ { matches = [ { node.name = "~.*" } ] '
            'actions = { update-props = { target.object = WRONG-SOURCE } } } ]')
        env = os.environ.copy()
        env.pop('PIPEWIRE_NO_CONFIG', None)
        env['HOME'] = str(home)
        env['XDG_CONFIG_HOME'] = str(home/'.config')
        env = isolate_client_environment(env, private)

        def parse(section):
            return subprocess.run([tool, '-N', 'merge', section], env=env,
                                  capture_output=True, text=True, check=True).stdout

        modules = parse('context.modules')
        assert 'filter-chain' not in modules and modules.count('libpipewire-module-') == 3
        assert 'WRONG-SOURCE' not in parse('stream.rules')
        (private/'filter.conf').write_text(json.dumps(config(Path('/fixture.so'), '110', 12)))
        env['PIPEWIRE_CONFIG_NAME'] = 'filter.conf'
        modules = parse('context.modules')
        assert 'filter-chain' in modules and '"110"' in modules
        assert 'WRONG-SOURCE' not in parse('stream.rules')
        print('Actual config parser: private client/filter configs load; user stream rules excluded')


if __name__ == '__main__':
    main(sys.argv[1])
