#!/usr/bin/env python3
"""Real synthetic-session and production-supervisor regression tests, CI only.

This starts private PipeWire/WirePlumber/D-Bus processes on disposable CI
runners, never hardware monitors. Production board guards remain unchanged:
the test bridge invokes the actual serve(), signal and ownership routines
below main(), with synthetic objects and a private state directory. It does
not qualify systemd user-manager behavior, hardware, or desktop GUI selection.
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
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
FALLBACK = 'j71b.test.other-source'
REMOTE = 'pipewire-0'


def load_runtime():
    spec = importlib.util.spec_from_file_location('desktop_mic', RUNTIME)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def props(obj):
    return obj.get('info', {}).get('props', obj.get('props', {}))


def nodes(data, name):
    return [o for o in data if o['type'].endswith(':Node')
            and props(o).get('node.name') == name]


def clean_environment(environment):
    return {k: v for k, v in environment.items()
            if not k.startswith(('PIPEWIRE_', 'SPA_', 'LD_', 'WIREPLUMBER_'))
            and k not in ('LADSPA_PATH', 'PULSE_SERVER')}


def require_ci():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() == 0:
        raise RuntimeError('Run only as an unprivileged user in the authorized disposable CI job')
    if Path('/dev/snd').exists():
        raise RuntimeError('Audio hardware must not be exposed to the synthetic test container')


def synthetic(name, priority):
    return {'factory': 'adapter', 'args': {
        'factory.name': 'audiotestsrc', 'node.name': name,
        'node.description': 'Synthetic J71b fixture, not hardware',
        'media.class': 'Audio/Source', 'audio.channels': 2,
        'audio.position': ['FL', 'FR'], 'audio.rate': 48000,
        'priority.session': priority,
        # Synthetic identity fixture, NOT an ALSA factory or device.
        'device.api': 'alsa', 'alsa.card_name': 'J71b Microphones',
        'api.alsa.pcm.stream': 'capture',
        'adapter.auto-port-config': {'mode': 'dsp', 'position': 'preserve'},
    }}


def server_config(runtime):
    return {'context.properties': {
        'core.daemon': True, 'core.name': REMOTE, 'default.clock.rate': 48000,
        'default.clock.quantum': 128, 'mem.allow-mlock': False},
        'context.spa-libs': {
            'audio.convert.*': 'audioconvert/libspa-audioconvert',
            'support.*': 'support/libspa-support',
            'audiotestsrc': 'audiotestsrc/libspa-audiotestsrc',
            'filter.graph': 'filter-graph/libspa-filter-graph'},
        'context.modules': [{'name': 'libpipewire-module-' + name} for name in
                            ('protocol-native', 'client-node', 'adapter',
                             'link-factory', 'spa-node-factory', 'metadata', 'access')],
        'context.objects': [
            {'factory': 'spa-node-factory', 'args': {
                'factory.name': 'support.node.driver', 'node.name': 'Dummy-Driver',
                'node.group': 'pipewire.dummy', 'priority.driver': 20000}},
            synthetic(runtime.SOURCE, 100), synthetic(FALLBACK, 2000)]}


class Session:
    def __init__(self, directory, plugin, wp_config):
        directory.mkdir(mode=0o700)
        self.root, self.plugin, self.wp_config = directory, plugin, wp_config
        self.processes, self.logs = [], []
        self.runtime = load_runtime()
        self.env = clean_environment(os.environ)
        self.env.update({
            'HOME': str(directory), 'XDG_RUNTIME_DIR': str(directory),
            'XDG_CONFIG_HOME': str(directory / 'config'),
            'XDG_STATE_HOME': str(directory / 'state'),
            'XDG_CACHE_HOME': str(directory / 'cache'),
            'PIPEWIRE_RUNTIME_DIR': str(directory), 'PIPEWIRE_REMOTE': REMOTE,
            'PIPEWIRE_CONFIG_DIR': str(directory),
            'WIREPLUMBER_CONFIG_DIR': str(directory / 'wireplumber')})

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, indent=2))
        return str(path)

    def start(self, name, command, stdout=None):
        log = (self.root / (name + '.log')).open('wb')
        self.logs.append(log)
        child = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=stdout if stdout is not None else log,
                                 stderr=log, start_new_session=True)
        self.processes.append(child)
        return child

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
    def stop(child, sig=signal.SIGINT, seconds=15):
        assert child.poll() is None, f'Process already exited: {child.returncode}'
        child.send_signal(sig)
        return child.wait(timeout=seconds)

    def close(self):
        # Fixture-only cleanup, after assertions. Every group was created by
        # this harness in the hardware-free CI container. Not service recovery.
        for child in reversed(self.processes):
            try:
                os.killpg(child.pid, signal.SIGCONT)
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for child in reversed(self.processes):
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for log in self.logs:
            log.close()
        # Never retain even synthetic audio in the diagnostic artifact.
        for path in self.root.glob('*.f32'):
            path.unlink()

    def boot(self):
        config = server_config(self.runtime)
        client = {'context.spa-libs': config['context.spa-libs'],
                  'context.modules': [{'name': 'libpipewire-module-' + name}
                                      for name in ('protocol-native', 'client-node', 'adapter')]}
        self.write('client.conf', client)
        self.daemon = self.start('daemon', ['pipewire', '-c', self.write('server.conf', config)])
        deadline = time.monotonic() + 5
        while not (self.root / REMOTE).exists():
            assert self.daemon.poll() is None, 'Private daemon failed to start'
            assert time.monotonic() < deadline, 'Private socket not created'
            time.sleep(.05)
        self.runtime.check_version(self.dump(), self.run('pw-dump', '--version'))
        (self.root / 'versions.txt').write_text(self.run('pw-dump', '--version')
                                                + self.run('wireplumber', '--version'))
        wp_dir = self.root / 'wireplumber'
        wp_dir.mkdir()
        (wp_dir / 'wireplumber.conf').write_text(self.wp_config.read_text() + '''
wireplumber.profiles = {
  j71b-test = {
    inherits = [ policy, mixin.systemwide-session ]
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
''')
        self.manager = self.start('wireplumber', ['wireplumber', '--profile=j71b-test'])
        data = self.await_graph(lambda d: default_source(d) == FALLBACK,
                                'WirePlumber selects synthetic fallback default')
        raw = nodes(data, self.runtime.SOURCE)[0]
        self.source_id, self.serial = raw['id'], str(props(raw)['object.serial'])
        self.run('pw-cli', 'set-param', str(self.source_id), 'Props', '{ volume: 0.02 }')
        self.write('initial-graph.json', self.dump())

    def supervisor(self, index):
        directory = self.root / f'supervisor-{index}'
        directory.mkdir()
        return self.start(f'supervisor-{index}', [sys.executable, str(Path(__file__).resolve()),
                          '--supervisor', str(directory), '--plugin', str(self.plugin)])

    @property
    def marker(self):
        return self.root / 'state/omarchy-j71b-microphone/ownership.json'

    def ready(self, child):
        def valid(data):
            assert child.poll() is None, 'Supervisor exited before becoming ready'
            try:
                self.runtime.validate_graph(data, self.serial)
                return True
            except (RuntimeError, self.runtime.PendingGraph):
                return False
        self.await_graph(valid, 'production supervisor publishes and links conditioned source')
        assert self.marker.is_file()
        check_idle(self)

    def removed(self):
        self.await_graph(lambda d: not nodes(d, self.runtime.OUTPUT)
                          and not nodes(d, self.runtime.CAPTURE), 'conditioned nodes removed')


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


def check_idle(session):
    def idle(data):
        return all(len(nodes(data, name)) == 1
                   and nodes(data, name)[0]['info']['state'] in ('idle', 'suspended')
                   for name in (session.runtime.SOURCE, session.runtime.CAPTURE, session.runtime.OUTPUT))
    session.await_graph(idle, 'raw and conditioned nodes idle', seconds=12)
    for _ in range(3):
        assert idle(session.dump()), 'Unexpected capture without consumers'
        time.sleep(.1)


def recorder(session, name, target, rate=48000, channels=2):
    path = session.root / (name + '.f32')
    stream = path.open('wb')
    child = session.start(name, [
        'pw-cat', '--record', '--raw', '--format=f32', f'--rate={rate}',
        f'--channels={channels}', f'--target={target}',
        '--properties={ node.name = j71b.test.' + name + ' node.dont-fallback = true }', '-'], stdout=stream)
    stream.close()
    return child, path


def finish_recording(session, child, path, rate, channels, bounded=True):
    # pw-cat 1.6.10's SIGINT path, like its recording sample limit, does not
    # signal drained. Only accept 1 after our requested signal, observed live
    # routing and independently validated synthetic PCM with no diagnostics.
    code = session.stop(child)
    assert code in (0, 1), f'Recorder failed: {code}'
    log = path.with_suffix('.log').read_text().strip()
    assert not log, 'Recorder diagnostics: ' + log
    samples = np.fromfile(path, dtype='<f4')
    assert samples.size >= rate * channels // 4, 'Too little synthetic audio'
    assert samples.size % channels == 0
    samples = samples.reshape(-1, channels)
    assert np.isfinite(samples).all()
    if bounded:
        assert np.max(np.abs(samples)) <= 10 ** (-1 / 20) + 1e-5
    rms = np.sqrt(np.mean(samples[-rate // 4:] ** 2, axis=0))
    assert np.all(rms > .001), f'Silent channel: {rms}'
    return {'frames': len(samples), 'rate': rate, 'channels': channels,
            'peak': float(np.max(np.abs(samples))), 'rms': rms.tolist(), 'exit': code}


def record(session, rate, channels, name):
    child, path = recorder(session, name, session.runtime.OUTPUT, rate, channels)
    session.await_graph(lambda d: nodes(d, session.runtime.OUTPUT)[0]['info']['state'] == 'running',
                        'recorder activates conditioned source')
    _, _, count, other = session.runtime.validate_graph(session.dump(), session.serial)
    assert count >= 1 and other == 0
    time.sleep(.6)
    result = finish_recording(session, child, path, rate, channels)
    check_idle(session)
    return result


def filter_pid(supervisor):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        children = Path(f'/proc/{supervisor.pid}/task/{supervisor.pid}/children').read_text().split()
        for child in children:
            try:
                argv = Path(f'/proc/{child}/cmdline').read_bytes().split(b'\0')
                if argv[:3] == [b'/usr/bin/pipewire', b'-c', b'filter.conf']:
                    return int(child)
            except FileNotFoundError:
                pass
        time.sleep(.05)
    raise AssertionError('Cannot identify our supervisor filter child')


def healthy(session):
    results = []
    for iteration in range(2):
        child = session.supervisor(iteration)
        session.ready(child)
        for index, (rate, channels) in enumerate(((48000, 2), (48000, 1), (44100, 2), (16000, 1))):
            results.append(record(session, rate, channels, f'app-{iteration}-{index}'))
            assert child.poll() is None
            assert default_source(session.dump()) == FALLBACK
        # A deliberate default selection must remain allowed.
        output_id = nodes(session.dump(), session.runtime.OUTPUT)[0]['id']
        session.run('wpctl', 'set-default', str(output_id))
        session.await_graph(lambda d: default_source(d) == session.runtime.OUTPUT, 'user-selected default')
        a, ap = recorder(session, f'concurrent-a-{iteration}', session.runtime.OUTPUT)
        b, bp = recorder(session, f'concurrent-b-{iteration}', session.runtime.OUTPUT, 16000, 1)
        raw, rp = recorder(session, f'concurrent-raw-{iteration}', session.runtime.SOURCE)
        session.await_graph(lambda d: session.runtime.validate_graph(d, session.serial)[2:] == (3, 2),
                            'simultaneous mono/stereo/raw consumers')
        time.sleep(.6)
        for proc, path, rate, channels, bounded in ((a, ap, 48000, 2, True),
                (b, bp, 16000, 1, True), (raw, rp, 48000, 2, False)):
            results.append(finish_recording(session, proc, path, rate, channels, bounded))
        check_idle(session)
        fallback_id = nodes(session.dump(), FALLBACK)[0]['id']
        session.run('wpctl', 'set-default', str(fallback_id))
        session.await_graph(lambda d: default_source(d) == FALLBACK, 'restore synthetic default')
        assert session.stop(child, signal.SIGTERM) == 0
        session.removed()
        assert not session.marker.exists(), 'Healthy shutdown left ownership marker'
    return results


def fault(session, mode):
    child = session.supervisor(mode)
    session.ready(child)
    pid = filter_pid(child)
    recorder(session, mode + '-consumer', session.runtime.OUTPUT)
    session.await_graph(lambda d: session.runtime.validate_graph(d, session.serial)[2] > 0
                        and nodes(d, session.runtime.OUTPUT)[0]['info']['state'] == 'running',
                        'consumer active before ' + mode)
    if mode == 'source-loss':
        session.run('pw-cli', 'destroy', str(session.source_id))
    elif mode == 'link-loss':
        session.manager.send_signal(signal.SIGSTOP)
        data = session.dump()
        capture_id = nodes(data, session.runtime.CAPTURE)[0]['id']
        link = next(o for o in data if o['type'].endswith(':Link')
                    and o['info']['input-node-id'] == capture_id)
        session.run('pw-cli', 'destroy', str(link['id']))
    elif mode == 'child-exit':
        os.kill(pid, signal.SIGTERM)
    elif mode == 'server-disconnect':
        session.stop(session.daemon)
    elif mode == 'hung-child':
        os.kill(pid, signal.SIGSTOP)
        child.send_signal(signal.SIGTERM)
    else:
        raise AssertionError(mode)
    deadline = time.monotonic() + 15
    while child.poll() is None:
        assert time.monotonic() < deadline, 'Supervisor did not exit after fault'
        if mode == 'source-loss':
            data = session.dump()
            capture = nodes(data, session.runtime.CAPTURE)
            if capture:
                assert all(o['info'].get('output-node-id') == session.source_id
                           for o in data if o['type'].endswith(':Link')
                           and o['info'].get('input-node-id') == capture[0]['id']), 'Fallback after source loss'
            assert default_source(data) == FALLBACK
        time.sleep(.05)
    assert child.returncode != 0, 'Fault was reported as clean shutdown'
    assert session.marker.is_file(), 'Fault cleared ownership marker'
    if mode == 'hung-child':
        # kill(pid, 0) would also accept a killed zombie. The same child must
        # remain stopped until the harness's separate fixture cleanup.
        status = Path(f'/proc/{pid}/status').read_text()
        state = next(line.split()[1] for line in status.splitlines() if line.startswith('State:'))
        assert state in ('T', 't'), 'Supervisor killed or resumed the stopped filter'
    if mode == 'link-loss':
        session.manager.send_signal(signal.SIGCONT)
    if mode != 'server-disconnect':
        data = session.dump()
        capture = nodes(data, session.runtime.CAPTURE)
        if capture:
            assert all(o['info'].get('output-node-id') == session.source_id
                       for o in data if o['type'].endswith(':Link')
                       and o['info'].get('input-node-id') == capture[0]['id'])
        session.write('fault-graph.json', data)
    # A new supervisor invocation must fail at the actual guard before opening
    # capture. Neither a new process nor a new config directory permits retry.
    retry = session.supervisor('blocked-retry')
    assert retry.wait(timeout=5) != 0
    assert session.marker.is_file()
    assert 'Previous microphone ownership was not cleared' in (session.root / 'supervisor-blocked-retry.log').read_text()
    return {'case': mode, 'exit': child.returncode, 'marker_retained': True,
            'restart_blocked': True}


def supervisor_bridge(directory, plugin):
    require_ci()
    runtime = load_runtime()
    state = Path(os.environ['XDG_STATE_HOME']) / 'omarchy-j71b-microphone'
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    env = clean_environment(os.environ)
    env.update(PIPEWIRE_RUNTIME_DIR=os.environ['XDG_RUNTIME_DIR'], PIPEWIRE_REMOTE=REMOTE)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, runtime.request_stop)
    with runtime.OwnershipGuard(state, boot) as guard:
        guard.check()
        runtime.serve(runtime.private_environment(env, directory), directory, plugin, guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated-session', action='store_true')
    parser.add_argument('--plugin', type=Path, default=Path(__file__).parent / 'build/j71b-capture.so')
    parser.add_argument('--wireplumber-config', type=Path, default=Path('/usr/share/wireplumber/wireplumber.conf'))
    parser.add_argument('--output-directory', type=Path)
    parser.add_argument('--inside-private-dbus', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--supervisor', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    require_ci()
    if args.supervisor:
        supervisor_bridge(args.supervisor.resolve(), args.plugin.resolve())
        return 0
    if not args.run_isolated_session or args.output_directory is None:
        parser.error('Explicit CI session opt-in and output directory required')
    for binary in ('pipewire', 'pw-dump', 'pw-cli', 'pw-cat', 'wpctl', 'wireplumber', 'dbus-run-session'):
        if not shutil.which(binary):
            parser.error('Required dependency missing: ' + binary)
    assert args.plugin.is_file() and args.wireplumber_config.is_file()
    if not args.inside_private_dbus:
        return subprocess.call(['dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
                                *sys.argv[1:], '--inside-private-dbus'])
    root = args.output_directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reports = []
    for name in ('healthy', 'source-loss', 'link-loss', 'child-exit', 'server-disconnect', 'hung-child'):
        session = Session(root / name, args.plugin.resolve(), args.wireplumber_config.resolve())
        try:
            session.boot()
            result = healthy(session) if name == 'healthy' else fault(session, name)
            reports.append({'case': name, 'status': 'passed', 'results': result})
            print('PASS:', name, flush=True)
        except Exception as error:
            reports.append({'case': name, 'status': 'failed', 'error': str(error)})
            for log in session.root.glob('*.log'):
                print(log.name + ':\n' + log.read_text(errors='replace')[-4000:], flush=True)
            graph = session.root / 'timeout-graph.json'
            if graph.exists():
                for obj in json.loads(graph.read_text()):
                    if obj['type'].endswith(('Node', 'Metadata', 'Link')):
                        print(json.dumps(obj), flush=True)
            raise
        finally:
            session.close()
            (root / 'results.json').write_text(json.dumps(reports, indent=2))
    print('PASS: real synthetic session and actual supervisor; no hardware or systemd-manager qualification')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
