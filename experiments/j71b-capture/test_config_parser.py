"""Check config isolation and module parsing; never load modules or connect."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hardware_test import config, isolate_client_environment


def check_module_serialization(source, directory, desktop):
    """Use the exact pinned parser with module loading replaced by a recorder."""
    expected = 'f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171'
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert revision == expected, 'Module parser test requires pinned PipeWire 1.6.10'
    subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD', '--',
                    'src/pipewire/conf.c', 'spa/include'], check=True, capture_output=True)
    conf = (source / 'src/pipewire/conf.c').read_text()
    begin = conf.index('static int parse_modules(')
    end = conf.index('\nstatic int create_object(', begin)
    # Preserve the complete upstream function verbatim, not a translated parser.
    (directory / 'pinned_parse_modules.inc').write_text(conf[begin:end])
    driver = Path(__file__).with_name('test_module_parser.c')
    executable = directory / 'test-module-parser'
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-I', str(source / 'spa/include'),
                    '-I', str(directory), str(driver), '-o', str(executable)], check=True)
    modules = desktop.filter_config(Path('/fixture.so'), '110')['context.modules']
    for label, indent in (('compact', None), ('pretty', 2)):
        result = subprocess.run([str(executable)], input=json.dumps(modules, indent=indent),
                                text=True, capture_output=True, check=True, timeout=5)
        fields = result.stdout.split('\0')
        assert fields[-1] == '' and len(fields) == 2 * len(modules) + 1, result.stdout
        for module, name, args in zip(modules, fields[0::2], fields[1::2]):
            assert name == module['name']
            assert (json.loads(args) if args else None) == module.get('args')
        if label == 'compact':
            assert result.stderr.startswith('malformed object array in '), result.stderr
        else:
            assert not result.stderr, result.stderr
        print(f'Pinned module parser: {label} JSON, {len(modules)} modules, '
              f'{"expected malformed-array warning" if result.stderr else "no diagnostics"}')


def main(tool, source):
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
        runtime = Path(__file__).resolve().parents[2] / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
        spec = importlib.util.spec_from_file_location('desktop_mic', runtime)
        desktop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(desktop)
        (private/'desktop.conf').write_text(json.dumps(desktop.filter_config(Path('/fixture.so'), '110')))
        env['PIPEWIRE_CONFIG_NAME'] = 'desktop.conf'
        modules = parse('context.modules')
        assert 'filter-chain' in modules and '"110"' in modules
        assert 'J71b Microphone (conditioned)' in modules
        assert 'WRONG-SOURCE' not in parse('stream.rules')
        print('Actual config parser: private acceptance/desktop configs load; user stream rules excluded')
        check_module_serialization(source, root, desktop)


if __name__ == '__main__':
    main(sys.argv[1], Path(sys.argv[2]).resolve())
