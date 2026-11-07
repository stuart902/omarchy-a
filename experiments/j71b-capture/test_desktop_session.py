#!/usr/bin/env python3
"""Pending, opt-in REAL PipeWire/WirePlumber regression harness.

NOT RUN OR QUALIFIED. This is not a mock and does not silently skip missing
binaries. It is deliberately not in automatic CI until the harness itself has
been validated. --run-isolated-session explicitly opts into starting a private
synthetic session. Syntax checking this file starts no processes.

Requires pipewire, pw-dump, pw-cli, pw-cat, wpctl, wireplumber >= 0.5,
dbus-run-session, NumPy, and a built j71b-capture.so. PATH/LD_LIBRARY_PATH,
SPA_PLUGIN_DIR and PIPEWIRE_MODULE_DIR may select an uninstalled build.
The session uses only synthetic generators, no ALSA or Bluetooth monitors.
It does NOT qualify hardware, desktop UI selection, or the service supervisor.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
FALLBACK = 'j71b.test.other-source'
REMOTE = 'j71b-synthetic-session'


def load_runtime():
    spec = importlib.util.spec_from_file_location('desktop_mic', RUNTIME)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def props(obj):
    return obj.get('info', {}).get('props', {})


def nodes(data, name):
    return [o for o in data if o['type'].endswith(':Node')
            and props(o).get('node.name') == name]


class Session:
    def __init__(self, directory):
        self.root = directory
        self.processes = []
        self.logs = []
        self.env = os.environ.copy()
        for key in ('PIPEWIRE_NO_CONFIG', 'PIPEWIRE_CONFIG_PREFIX',
                    'PIPEWIRE_CONFIG_NAME', 'WIREPLUMBER_CONFIG_DIR',
                    'WIREPLUMBER_CONFIG_NAME', 'PULSE_SERVER'):
            self.env.pop(key, None)
        self.env.update({
            'HOME': str(directory), 'XDG_RUNTIME_DIR': str(directory),
            'XDG_CONFIG_HOME': str(directory / 'config'),
            'XDG_STATE_HOME': str(directory / 'state'),
            'XDG_CACHE_HOME': str(directory / 'cache'),
            'PIPEWIRE_RUNTIME_DIR': str(directory),
            'PIPEWIRE_REMOTE': REMOTE,
            'PIPEWIRE_CONFIG_DIR': str(directory),
            'WIREPLUMBER_CONFIG_DIR': str(directory / 'wireplumber'),
        })

    def write(self, name, config):
        path = self.root / name
        path.write_text(json.dumps(config, indent=2))
        return str(path)

    def start(self, name, command, stdout=None):
        log = (self.root / (name + '.log')).open('wb')
        self.logs.append(log)
        process = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=stdout or log, stderr=log)
        self.processes.append(process)
        return process

    def run(self, *command):
        result = subprocess.run(command, env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise AssertionError(f'{command}: {result.stderr}')
        return result.stdout

    def dump(self):
        return json.loads(self.run('pw-dump'))

    def await_graph(self, predicate, description, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = self.dump()
            if predicate(data):
                return data
            time.sleep(.1)
        self.write('timeout-graph.json', data)
        raise AssertionError('Timeout: ' + description)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for process in reversed(self.processes):
            self.stop(process)
        for log in self.logs:
            log.close()


def synthetic(name, priority):
    return {'factory': 'adapter', 'args': {
        'factory.name': 'audiotestsrc', 'node.name': name,
        'node.description': 'Synthetic J71b lifecycle fixture, not hardware',
        'media.class': 'Audio/Source', 'audio.channels': 2,
        'audio.position': ['FL', 'FR'], 'audio.rate': 48000,
        'priority.session': priority,
        # Identity properties exercise the production guard, but the factory
        # remains audiotestsrc; no ALSA device or ALSA module is created.
        'device.api': 'alsa', 'alsa.card_name': 'J71b Microphones',
        'api.alsa.pcm.stream': 'capture',
        'adapter.auto-port-config': {'mode': 'dsp', 'position': 'preserve'},
    }}


def check_idle(session, runtime):
    def idle(data):
        return all(len(nodes(data, name)) == 1
                   and nodes(data, name)[0]['info']['state'] != 'running'
                   for name in (runtime.SOURCE, runtime.CAPTURE, runtime.OUTPUT))
    session.await_graph(idle, 'passive raw and conditioned nodes idle')
    # A single sample can miss an unintended periodic wakeup.
    for _ in range(10):
        assert idle(session.dump()), 'Unexpected capture while no app is consuming'
        time.sleep(.1)


def default_source(data):
    for obj in data:
        if obj['type'].endswith(':Metadata') and props(obj).get('metadata.name') == 'default':
            for entry in obj.get('metadata', []):
                if entry.get('key') == 'default.audio.source':
                    value = entry['value']
                    if isinstance(value, str):
                        value = json.loads(value)
                    return value.get('name')
    return None


def assert_no_foreign_links(data, runtime, source_id):
    capture = nodes(data, runtime.CAPTURE)
    if not capture:
        return
    for link in data:
        if link['type'].endswith(':Link'):
            info = link.get('info', {})
            if info.get('input-node-id') == capture[0]['id']:
                assert info.get('output-node-id') == source_id, 'Capture fell back to another source'


def record(session, runtime, source_serial, rate, channels, index):
    path = session.root / f'record-{index}-{rate}-{channels}.f32'
    with path.open('wb') as output:
        process = session.start(f'app-{index}', [
            'pw-cat', '--record', '--raw', '--format=f32', f'--rate={rate}',
            f'--channels={channels}', f'--target={runtime.OUTPUT}',
            '--properties={ node.name = j71b.test.recorder node.dont-fallback = true }', '-'],
            stdout=output)
        session.await_graph(lambda d: nodes(d, runtime.OUTPUT)
                            and nodes(d, runtime.OUTPUT)[0]['info']['state'] == 'running',
                            'ordinary recorder activates conditioned source')
        data = session.dump()
        raw, conditioned, consumers, other_raw = runtime.validate_graph(
            data, source_serial, require_links=True)
        assert consumers >= 1, 'Recorder has no conditioned-source links'
        assert other_raw == 0, 'Recorder bypassed conditioning'
        time.sleep(.65)
        assert process.poll() is None, 'Recorder exited unexpectedly'
        session.stop(process)
        assert process.returncode == 0, f'Recorder failed: {process.returncode}'
    samples = np.fromfile(path, dtype='<f4')
    assert samples.size >= rate * channels // 4, 'Recorder received too little audio'
    assert samples.size % channels == 0
    samples = samples.reshape(-1, channels)
    assert np.isfinite(samples).all(), 'Nonfinite desktop audio'
    assert np.max(np.abs(samples)) <= 10 ** (-1 / 20) + 1e-5, 'Clamp exceeded'
    assert np.all(np.sqrt(np.mean(samples[-rate // 4:] ** 2, axis=0)) > .001), 'Silent channel'
    # Exact plugin reset/fade shape is covered separately by spa_graph.py.
    # Client scheduling may start delivery after the fade, so do not claim that
    # repeated recorder reads alone prove the activation envelope.
    check_idle(session, runtime)


def exercise(directory, plugin, wireplumber_config):
    runtime = load_runtime()
    session = Session(directory)
    try:
        spa = {'audio.convert.*': 'audioconvert/libspa-audioconvert',
               'support.*': 'support/libspa-support',
               'audiotestsrc': 'audiotestsrc/libspa-audiotestsrc',
               'filter.graph': 'filter-graph/libspa-filter-graph'}
        client_modules = [{'name': 'libpipewire-module-' + name}
                          for name in ('protocol-native', 'client-node', 'adapter')]
        session.write('client.conf', {'context.spa-libs': spa,
                                      'context.modules': client_modules})
        server = {'context.properties': {
            'core.daemon': True, 'core.name': REMOTE, 'default.clock.rate': 48000,
            'default.clock.quantum': 128, 'mem.allow-mlock': False},
            'context.spa-libs': spa,
            'context.modules': [{'name': 'libpipewire-module-' + name} for name in
                                ('protocol-native', 'client-node', 'adapter',
                                 'link-factory', 'spa-node-factory', 'metadata', 'access')],
            'context.objects': [synthetic(runtime.SOURCE, 100), synthetic(FALLBACK, 2000)]}
        daemon = session.start('daemon', ['pipewire', '-c', session.write('server.conf', server)])
        deadline = time.monotonic() + 5
        while not (directory / REMOTE).exists():
            assert daemon.poll() is None, 'Private daemon failed to start'
            assert time.monotonic() < deadline, 'Private socket not created'
            time.sleep(.05)
        runtime.check_version(session.dump(), session.run('pw-dump', '--version'))
        print(session.run('pw-dump', '--version').strip())
        print(session.run('wireplumber', '--version').strip())
        # Copy only the explicit vendor base config, never host/user fragments.
        # Force hardware features disabled even if a vendor changes policy.
        wp_dir = directory / 'wireplumber'
        wp_dir.mkdir()
        (wp_dir / 'wireplumber.conf').write_text(wireplumber_config.read_text() + """
wireplumber.profiles = {
  j71b-test = {
    inherits = [ policy ]
    hardware.audio = disabled
    hardware.bluetooth = disabled
    hardware.video-capture = disabled
    monitor.alsa = disabled
    monitor.alsa-midi = disabled
    monitor.bluez = disabled
    monitor.bluez-midi = disabled
    monitor.v4l2 = disabled
    monitor.libcamera = disabled
  }
}
""")
        manager = session.start('wireplumber', ['wireplumber', '--profile=j71b-test'])
        data = session.await_graph(lambda d: default_source(d) == FALLBACK,
                                   'WirePlumber selects synthetic fallback default')
        raw = nodes(data, runtime.SOURCE)[0]
        source_id = raw['id']
        serial = str(props(raw)['object.serial'])
        session.run('pw-cli', 'set-param', str(source_id), 'Props', '{ volume: 0.02 }')
        for iteration in range(3):
            config = runtime.filter_config(plugin, serial, gain=12)
            chain = session.start(f'conditioner-{iteration}',
                                  ['pipewire', '-c', session.write('filter.conf', config)])
            session.await_graph(lambda d: len(nodes(d, runtime.OUTPUT)) == 1,
                                'conditioned microphone published exactly once')
            check_idle(session, runtime)
            for index, (rate, channels) in enumerate(((48000, 2), (48000, 1), (44100, 2), (16000, 1))):
                record(session, runtime, serial, rate, channels, iteration * 4 + index)
                assert default_source(session.dump()) == FALLBACK, 'Default microphone changed'
            assert manager.poll() is None, 'WirePlumber exited'
            session.stop(chain)
            session.await_graph(lambda d: not nodes(d, runtime.OUTPUT)
                                and not nodes(d, runtime.CAPTURE), 'no nodes after conditioner stop')
        # Test stale serial/no-fallback both after removal and from cold start.
        for iteration in range(2):
            chain = session.start(f'missing-source-{iteration}', ['pipewire', '-c', str(directory / 'filter.conf')])
            session.await_graph(lambda d: nodes(d, runtime.OUTPUT), 'missing-source fixture published')
            if iteration == 0:
                session.run('pw-cli', 'destroy', str(source_id))
                session.await_graph(lambda d: not nodes(d, runtime.SOURCE), 'raw source removed')
            consumer = session.start(f'missing-app-{iteration}', [
                'pw-cat', '--record', '--raw', '--format=f32', '--channels=2',
                f'--target={runtime.OUTPUT}', '--properties={ node.dont-fallback = true }', '-'])
            for _ in range(20):
                data = session.dump()
                assert_no_foreign_links(data, runtime, source_id)
                assert default_source(data) == FALLBACK
                time.sleep(.1)
            session.stop(consumer)
            session.stop(chain)
            session.await_graph(lambda d: not nodes(d, runtime.OUTPUT), 'missing-source fixture removed')
        print('PASS: synthetic real-session routing, app formats, idle, recreation, removal, no fallback')
        print('NOT TESTED: hardware, supervisor shutdown, or per-transition sample-accurate fade/reset')
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated-session', action='store_true',
                        help='Run only after authorization; not an approval mechanism')
    parser.add_argument('--plugin', type=Path, default=Path(__file__).parent / 'build/j71b-capture.so')
    parser.add_argument('--wireplumber-config', type=Path,
                        default=Path('/usr/share/wireplumber/wireplumber.conf'),
                        help='Vendor base configuration; no .d fragments are loaded')
    parser.add_argument('--inside-private-dbus', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.run_isolated_session:
        parser.error('Real-session execution is opt-in and pending authorization/denial clarification')
    for binary in ('pipewire', 'pw-dump', 'pw-cli', 'pw-cat', 'wpctl', 'wireplumber', 'dbus-run-session'):
        if not shutil.which(binary):
            parser.error('Required real-session dependency missing: ' + binary)
    if not args.wireplumber_config.resolve().is_file():
        parser.error('WirePlumber vendor base configuration not found')
    if not args.plugin.resolve().is_file():
        parser.error('Build the LADSPA plugin first')
    if not args.inside_private_dbus:
        return subprocess.call(['dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
                                *sys.argv[1:], '--inside-private-dbus'])
    # mkdtemp is mode 0700; retain logs on failure for honest qualification.
    directory = Path(tempfile.mkdtemp(prefix='j71b-synthetic-'))
    print('Private synthetic session logs:', directory, flush=True)
    exercise(directory, args.plugin.resolve(), args.wireplumber_config.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
