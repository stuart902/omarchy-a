#!/usr/bin/env python3
"""Real synthetic-session and production-supervisor regression tests, CI only.

This starts private PipeWire/WirePlumber/PulseAudio-protocol/D-Bus processes on disposable CI
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
import traceback

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
FALLBACK = 'j71b.test.other-source'
REMOTE = 'pipewire-0'
DRIVER = 'Dummy-Driver'
# Pinned audiotestsrc default. pw-cli serializes SPA_PROP_frequency as Int,
# while this source accepts Float, so a named JSON frequency update is ignored.
TONE_FREQUENCY = 440
# audiotestsrc's volume is also applied by its adapter: .1 * .1. A different
# right-channel amplitude makes swapped/dropped channels observable in PCM.
TONE_AMPLITUDES = np.array([.01, .005])


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
            if not k.startswith(('PIPEWIRE_', 'SPA_', 'LD_', 'WIREPLUMBER_', 'PULSE_'))
            and k != 'LADSPA_PATH'}


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
        'default.clock.quantum': 128, 'mem.allow-mlock': False, 'log.level': 3},
        'context.spa-libs': {
            'audio.convert.*': 'audioconvert/libspa-audioconvert',
            'support.*': 'support/libspa-support',
            'audiotestsrc': 'audiotestsrc/libspa-audiotestsrc',
            'filter.graph': 'filter-graph/libspa-filter-graph'},
        'context.modules': [{'name': 'libpipewire-module-' + name} for name in
                            ('protocol-native', 'client-node', 'adapter',
                             'link-factory', 'spa-node-factory', 'metadata', 'access')],
        # The test generator's live-driver timer stays stopped after buffer
        # starvation in PipeWire 1.6.10. Use its supported follower mode with
        # the normal dummy clock. Rules override the factory's advertised
        # driver=true; adapter args alone are overwritten by that advertisement.
        'node.rules': [{'matches': [{'node.name': runtime.SOURCE}, {'node.name': FALLBACK}],
                        'actions': {'update-props': {'node.driver': False}}}],
        'context.objects': [
            {'factory': 'spa-node-factory', 'args': {
                'factory.name': 'support.node.driver', 'node.name': DRIVER,
                'node.group': 'pipewire.dummy', 'priority.driver': 20000}},
            synthetic(runtime.SOURCE, 100), synthetic(FALLBACK, 2000)]}


def client_config(config):
    # pw-dump must load the metadata protocol marshal to bind and report
    # WirePlumber's default metadata, just like the upstream client.conf.
    return {'context.spa-libs': config['context.spa-libs'],
            'context.modules': [{'name': 'libpipewire-module-' + name}
                                for name in ('protocol-native', 'client-node',
                                             'adapter', 'metadata')]}


def pulse_config(config, socket):
    result = client_config(config)
    result['context.properties'] = {'core.daemon': False, 'mem.allow-mlock': False}
    result['context.modules'].append({'name': 'libpipewire-module-protocol-pulse'})
    result['pulse.properties'] = {'server.address': ['unix:' + str(socket)]}
    # No TCP listener, discovery, hardware, default switching, module autoload,
    # or service units. This bridge uses the same private native server.
    result['pulse.cmd'] = []
    return result


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
        if result.stderr:
            with (self.root / 'commands.log').open('a') as log:
                log.write(repr(command) + '\n' + result.stderr + '\n')
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
        self.write('client.conf', client_config(config))
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
        for name in (self.runtime.SOURCE, FALLBACK):
            assert props(nodes(data, name)[0]).get('node.driver') is False
        self.source_id, self.serial = raw['id'], str(props(raw)['object.serial'])
        # The adapter forwards volume to audiotestsrc and also applies it.
        # Leave ample nonzero signal for both raw and +12 dB conditioned PCM.
        self.run('pw-cli', 'set-param', str(self.source_id), 'Props', '{ volume: 0.1 }')
        self.write('initial-graph.json', self.dump())

    def supervisor(self, index):
        directory = self.root / f'supervisor-{index}'
        directory.mkdir()
        return self.start(f'supervisor-{index}', [sys.executable, str(Path(__file__).resolve()),
                          '--supervisor', str(directory), '--plugin', str(self.plugin)])

    def boot_pulse(self):
        socket = self.root / 'pulse-native'
        client = self.root / 'pulse-client.conf'
        client.write_text('autospawn = no\n')
        self.env.update(PULSE_SERVER='unix:' + str(socket), PULSE_CLIENTCONFIG=str(client))
        config = pulse_config(server_config(self.runtime), socket)
        self.pulse = self.start('pipewire-pulse', [
            'pipewire-pulse', '-c', self.write('pulse.conf', config)])
        deadline = time.monotonic() + 5
        while not socket.exists():
            assert self.pulse.poll() is None, 'Private PulseAudio-protocol bridge failed to start'
            assert time.monotonic() < deadline, 'Private PulseAudio socket not created'
            time.sleep(.05)
        self.await_pulse_sources({self.runtime.SOURCE, FALLBACK})
        (self.root / 'pulse-versions.txt').write_text(
            self.run('pipewire-pulse', '--version') + self.run('pactl', '--version')
            + self.run('parec', '--version') + self.run('pactl', 'info'))

    def pulse_sources(self):
        return json.loads(self.run('pactl', '--format=json', 'list', 'sources'))

    def await_pulse_sources(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            sources = self.pulse_sources()
            if {source['name'] for source in sources} == expected:
                assert len(sources) == len(expected), 'Duplicate PulseAudio source'
                return sources
            time.sleep(.05)
        self.write('pulse-source-timeout.json', sources)
        raise AssertionError('PulseAudio sources differ from ' + repr(expected))

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
                   for name in (session.runtime.SOURCE, session.runtime.CAPTURE, session.runtime.OUTPUT, DRIVER))
    session.await_graph(idle, 'raw and conditioned nodes idle', seconds=12)
    for _ in range(3):
        assert idle(session.dump()), 'Unexpected capture without consumers'
        time.sleep(.1)


def recorder(session, name, target, rate=48000, channels=2):
    path = session.root / (name + '.f32')
    stream = path.open('wb')
    properties = {'node.name': 'j71b.test.' + name}
    if target is not None:
        # Pin explicit-target fixtures. With dont-fallback alone, WP's
        # follow-default policy changes target.node to -1 and rejects them.
        properties.update({'node.dont-fallback': True, 'node.dont-reconnect': True})
    child = session.start(name, [
        'pw-cat', '--record', '--raw', '--format=f32', f'--rate={rate}',
        f'--channels={channels}', *([f'--target={target}'] if target is not None else []),
        '--properties=' + json.dumps(properties), '-'], stdout=stream)
    stream.close()
    return child, path


def await_pcm(session, child, path, rate, channels):
    # Hosted-runner scheduling is not a real-time guarantee. Wait for observed
    # PCM instead of treating a short wall-clock sleep as captured duration.
    deadline = time.monotonic() + 5
    while path.stat().st_size < rate * channels * 4 // 2:
        assert child.poll() is None, f'{path.name}: recorder exited before enough PCM'
        if time.monotonic() >= deadline:
            session.write('pcm-timeout-graph.json', session.dump())
            session.write('pcm-timeout.json', {'recording': path.name,
                          'bytes': path.stat().st_size, 'rate': rate, 'channels': channels})
            raise AssertionError(f'{path.name}: timed out waiting for PCM ({path.stat().st_size} bytes)')
        time.sleep(.05)


def finish_recording(session, child, path, rate, channels, bounded=True, accepted_codes=(0, 1)):
    # pw-cat 1.6.10's SIGINT path, like its recording sample limit, does not
    # signal drained. Only accept 1 after our requested signal, observed live
    # routing and independently validated synthetic PCM with no diagnostics.
    # libpulse's parec has a clean SIGINT path and must return 0 instead.
    await_pcm(session, child, path, rate, channels)
    code = session.stop(child)
    assert code in accepted_codes, f'Recorder failed: {code}'
    log = path.with_suffix('.log').read_text().strip()
    assert not log, 'Recorder diagnostics: ' + log
    samples = np.fromfile(path, dtype='<f4')
    assert samples.size >= rate * channels // 4, f'{path.name}: too little synthetic audio'
    assert samples.size % channels == 0
    samples = samples.reshape(-1, channels)
    assert np.isfinite(samples).all()
    if bounded:
        assert np.max(np.abs(samples)) <= 10 ** (-1 / 20) + 1e-5
    rms = np.sqrt(np.mean(samples[-rate // 4:] ** 2, axis=0))
    assert np.all(rms > .001), f'{path.name}: silent channel: {rms}'
    result = {'frames': len(samples), 'rate': rate, 'channels': channels,
              'peak': float(np.max(np.abs(samples))), 'rms': rms.tolist(), 'exit': code}
    session.write('recording-summary-' + path.stem + '.json', result)
    return result


def record(session, rate, channels, name, use_default=False):
    child, path = recorder(session, name, None if use_default else session.runtime.OUTPUT, rate, channels)
    session.await_graph(lambda d: nodes(d, session.runtime.OUTPUT)[0]['info']['state'] == 'running',
                        'recorder activates conditioned source')
    graph = session.dump()
    raw, _, count, other = session.runtime.validate_graph(graph, session.serial)
    assert str(props(raw).get('node.driver-id')) == str(nodes(graph, DRIVER)[0]['id'])
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


def consumer_routes_ready(data, routes, companions):
    """Check actual adapter ports; mono PCM may use stereo external DSP ports."""
    names = set(routes) | set(routes.values()) | set(companions) | set(companions.values())
    selected = {name: nodes(data, name) for name in names}
    if any(len(found) != 1 for found in selected.values()):
        return False
    selected = {name: found[0] for name, found in selected.items()}
    links = [o['info'] for o in data if o['type'].endswith(':Link')]
    expected = {}
    for consumer, source in routes.items():
        node, source_id = selected[consumer], selected[source]['id']
        expected.setdefault(source_id, set()).add(node['id'])
        ports = {o['id'] for o in data if o['type'].endswith(':Port')
                 and str(props(o).get('node.id')) == str(node['id'])
                 and o.get('info', {}).get('direction') == 'input'
                 and props(o).get('audio.channel')}
        incoming = [link for link in links if link['input-node-id'] == node['id']]
        if not ports or node['info']['state'] != 'running' or len(incoming) != len(ports):
            return False
        if {link['input-port-id'] for link in incoming} != ports:
            return False
        for link in incoming:
            assert link['output-node-id'] == source_id, 'Recorder connected to wrong source'
            assert not link.get('error'), 'Recorder link error'
            if link['state'] != 'active':
                return False
    for consumer, source in companions.items():
        expected.setdefault(selected[source]['id'], set()).add(selected[consumer]['id'])
    return all({link['input-node-id'] for link in links if link['output-node-id'] == source}
               == destinations for source, destinations in expected.items())


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
        results.append(record(session, 48000, 2, f'default-{iteration}', use_default=True))
        a, ap = recorder(session, f'concurrent-a-{iteration}', session.runtime.OUTPUT)
        b, bp = recorder(session, f'concurrent-b-{iteration}', session.runtime.OUTPUT, 16000, 1)
        raw, rp = recorder(session, f'concurrent-raw-{iteration}', session.runtime.SOURCE)
        def concurrent(data):
            session.runtime.validate_graph(data, session.serial)
            return consumer_routes_ready(data, {
                f'j71b.test.concurrent-a-{iteration}': session.runtime.OUTPUT,
                f'j71b.test.concurrent-b-{iteration}': session.runtime.OUTPUT,
                f'j71b.test.concurrent-raw-{iteration}': session.runtime.SOURCE,
            }, {session.runtime.CAPTURE: session.runtime.SOURCE})
        session.await_graph(concurrent,
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
        assert 'malformed object array' not in (session.root / f'supervisor-{iteration}.log').read_text()
        session.removed()
        assert not session.marker.exists(), 'Healthy shutdown left ownership marker'
    return results


def pulse_recorder(session, name, target, rate=48000, channels=2, pinned=False):
    path = session.root / (name + '.f32')
    command = ['parec', '--raw', '--format=float32le', f'--rate={rate}',
               f'--channels={channels}', '--latency-msec=50',
               '--channel-map=' + ('mono' if channels == 1 else 'front-left,front-right'),
               '--property=node.name=j71b.test.' + name]
    if target is not None:
        command.append('--device=' + target)
    if pinned:
        assert target is not None
        # Explicit opt-in stream hints, not a guarantee about every desktop
        # app. Ordinary clients may intentionally follow the session default.
        command.extend(['--property=node.dont-fallback=true',
                        '--property=node.dont-reconnect=true'])
    with path.open('wb') as stream:
        child = session.start(name, command, stdout=stream)
    return child, path


def pulse_route(session, name, source, channels):
    consumer = 'j71b.test.' + name
    def routed(data):
        session.runtime.validate_graph(data, session.serial)
        companions = {session.runtime.CAPTURE: session.runtime.SOURCE}
        if not consumer_routes_ready(data, {consumer: source}, companions):
            return False
        if channels == 2:
            ident = nodes(data, consumer)[0]['id']
            ports = {o['id']: props(o) for o in data if o['type'].endswith(':Port')}
            mappings = [(ports[o['info']['output-port-id']]['audio.channel'],
                         ports[o['info']['input-port-id']]['audio.channel'])
                        for o in data if o['type'].endswith(':Link')
                        and o['info']['input-node-id'] == ident]
            assert sorted(mappings) == [('FL', 'FL'), ('FR', 'FR')], 'Crossed PulseAudio stereo ports'
        return True
    session.await_graph(routed, 'libpulse client receives selected source: ' + name)
    # Verify the source index visible to actual PulseAudio clients too, not
    # just native graph properties or a matching node name.
    sources = session.pulse_sources()
    source_index = next(item['index'] for item in sources if item['name'] == source)
    streams = json.loads(session.run('pactl', '--format=json', 'list', 'source-outputs'))
    selected = [stream for stream in streams if stream['properties'].get('node.name') == consumer]
    assert len(selected) == 1 and selected[0]['source'] == source_index, 'Wrong PulseAudio source index'
    session.write('pulse-route-' + name + '.json', selected[0])


def tone_expectation(channels, conditioned):
    amplitudes = TONE_AMPLITUDES if channels == 2 else np.array([TONE_AMPLITUDES.mean()])
    # Independent steady-state response of the production 80 Hz Butterworth
    # high-pass at its fixed 48 kHz graph rate, then its unchanged +12 dB.
    # Transport delay/phase is unknown. The last quarter second is well past
    # the 20 ms startup fade and gain smoothing, including resampled clients.
    relative = np.tan(np.pi * 80 / 48000) / np.tan(np.pi * TONE_FREQUENCY / 48000)
    gain = 10 ** (12 / 20) / np.sqrt(1 + relative ** 4) if conditioned else 1
    return amplitudes * gain


def check_tone(samples, rate, channels, conditioned):
    assert samples.ndim == 2 and samples.shape[1] == channels
    assert len(samples) >= rate // 2, 'Too little PCM for a settled tone'
    assert np.isfinite(samples).all(), 'Nonfinite synthetic PCM'
    tail = samples[-rate // 4:].astype(np.float64)
    phase = 2 * np.pi * TONE_FREQUENCY * np.arange(len(tail)) / rate
    basis = np.column_stack((np.sin(phase), np.cos(phase), np.ones(len(tail))))
    coefficients = np.linalg.lstsq(basis, tail, rcond=None)[0]
    amplitude = np.hypot(coefficients[0], coefficients[1])
    rms = np.sqrt(np.mean(tail ** 2, axis=0))
    expected = tone_expectation(channels, conditioned)
    # Tolerance permits float-generator drift, resampling and runner timing,
    # but rejects unity gain, +24 dB, missing/swapped channels or raw fallback.
    np.testing.assert_allclose(amplitude, expected, rtol=.04, atol=1e-5)
    np.testing.assert_allclose(rms, expected / np.sqrt(2), rtol=.04, atol=1e-5)
    residual = np.sqrt(np.mean((tail - basis @ coefficients) ** 2, axis=0))
    assert np.all(residual < expected * .03), 'Wrong tone, distortion or dropped PCM'
    assert np.all(np.abs(coefficients[2]) < expected * .01), 'Unexpected DC'
    if channels == 2:
        assert np.corrcoef(tail.T)[0, 1] > .995, 'Stereo polarity or channel content differs'
    return {'expected_amplitude': expected.tolist(), 'amplitude': amplitude.tolist(),
            'expected_rms': (expected / np.sqrt(2)).tolist(), 'rms': rms.tolist(),
            'residual_rms': residual.tolist(), 'frequency': TONE_FREQUENCY,
            'gain_db': 12 if conditioned else 0}


def pulse_record(session, name, source, rate, channels, use_default=False):
    child, path = pulse_recorder(session, name, None if use_default else source, rate, channels)
    pulse_route(session, name, source, channels)
    result = finish_recording(session, child, path, rate, channels, accepted_codes=(0,))
    result['tone'] = check_tone(np.fromfile(path, dtype='<f4').reshape(-1, channels),
                                rate, channels, source == session.runtime.OUTPUT)
    session.write('recording-summary-' + name + '.json', result)
    check_idle(session)
    return result


def assert_pulse_default(session, source):
    session.await_graph(lambda data: default_source(data) == source, 'native default remains ' + source)
    assert session.run('pactl', 'get-default-source').strip() == source, 'PulseAudio default changed'


def assert_no_consumer_fallback(data, name, allowed_source=None):
    consumers = {node['id'] for node in nodes(data, 'j71b.test.' + name)}
    sources = {node['id'] for node in nodes(data, allowed_source)} if allowed_source else set()
    for obj in data:
        if obj['type'].endswith(':Link') and obj['info']['input-node-id'] in consumers:
            assert obj['info']['output-node-id'] in sources, 'Pinned libpulse client fell back'


def pulse_absent(session, name):
    session.await_pulse_sources({session.runtime.SOURCE, FALLBACK})
    child, path = pulse_recorder(session, name, session.runtime.OUTPUT, pinned=True)
    deadline = time.monotonic() + 5
    while child.poll() is None:
        assert_no_consumer_fallback(session.dump(), name)
        assert time.monotonic() < deadline, 'Missing named source was not rejected'
        time.sleep(.05)
    assert child.returncode != 0, 'Missing named source reported success'
    assert path.stat().st_size == 0, 'Missing named source produced PCM'
    assert_no_consumer_fallback(session.dump(), name)
    assert_pulse_default(session, FALLBACK)
    return {'case': name, 'exit': child.returncode, 'pcm_bytes': 0, 'no_fallback': True}


def configure_pulse_tone(session):
    # Only fixture input changes, never the service gain or DSP. Retain the
    # pinned generator's default frequency and verify it rather than assuming
    # that a successful pw-cli exit means every optional property took effect.
    session.run('pw-cli', 'set-param', str(session.source_id), 'Props',
                '{ volume: 0.1, channelVolumes: [ 1.0, 0.5 ] }')
    raw = nodes(session.dump(), session.runtime.SOURCE)[0]
    parameters = raw['info']['params']['Props']
    generators = [param for param in parameters if 'frequency' in param]
    assert len(generators) == 1, 'Cannot verify synthetic generator frequency'
    assert generators[0]['frequency'] == TONE_FREQUENCY, 'Unexpected synthetic generator frequency'
    assert generators[0]['waveType'] == 0, 'Synthetic generator is not a sine wave'
    session.write('pulse-fixture-props.json', parameters)


def pulse_healthy(session):
    session.boot_pulse()
    assert_pulse_default(session, FALLBACK)
    results = [pulse_absent(session, 'pulse-absent-before')]
    child = session.supervisor('pulse')
    session.ready(child)
    sources = session.await_pulse_sources({session.runtime.SOURCE, session.runtime.OUTPUT, FALLBACK})
    output = next(source for source in sources if source['name'] == session.runtime.OUTPUT)
    assert output['description'] == 'J71b Microphone (conditioned)'
    assert output['channel_map'] == 'front-left,front-right'
    session.write('pulse-sources.json', sources)
    assert_pulse_default(session, FALLBACK)
    configure_pulse_tone(session)
    results.append(pulse_record(session, 'pulse-raw', session.runtime.SOURCE, 48000, 2))
    for rate, channels in ((48000, 2), (48000, 1), (44100, 2), (16000, 1)):
        results.append(pulse_record(session, f'pulse-{rate}-{channels}',
                                    session.runtime.OUTPUT, rate, channels))
        assert child.poll() is None
        assert_pulse_default(session, FALLBACK)
    # A normal PulseAudio client without --device follows a deliberate choice.
    session.run('pactl', 'set-default-source', session.runtime.OUTPUT)
    assert_pulse_default(session, session.runtime.OUTPUT)
    results.append(pulse_record(session, 'pulse-default', session.runtime.OUTPUT, 48000, 2, True))
    session.run('pactl', 'set-default-source', FALLBACK)
    assert_pulse_default(session, FALLBACK)
    # A source-pinned client already recording must fail when the conditioned
    # source is removed, rather than move to the raw or other default source.
    name = 'pulse-stop-consumer'
    consumer, path = pulse_recorder(session, name, session.runtime.OUTPUT, pinned=True)
    pulse_route(session, name, session.runtime.OUTPUT, 2)
    await_pcm(session, consumer, path, 48000, 2)
    assert session.stop(child, signal.SIGTERM) == 0
    deadline = time.monotonic() + 5
    while consumer.poll() is None:
        assert_no_consumer_fallback(session.dump(), name, session.runtime.OUTPUT)
        assert time.monotonic() < deadline, 'Pinned client remained connected after source removal'
        time.sleep(.05)
    assert consumer.returncode != 0, 'Source removal reported success to the pinned client'
    result = check_tone(np.fromfile(path, dtype='<f4').reshape(-1, 2), 48000, 2, True)
    results.append({'case': 'pulse-stop-consumer', 'exit': consumer.returncode,
                    'no_fallback': True, 'tone': result})
    session.removed()
    assert not session.marker.exists(), 'PulseAudio-client shutdown left ownership marker'
    assert_no_consumer_fallback(session.dump(), name)
    results.append(pulse_absent(session, 'pulse-absent-after'))
    session.await_graph(lambda data: all(nodes(data, name)[0]['info']['state'] in ('idle', 'suspended')
                        for name in (session.runtime.SOURCE, FALLBACK, DRIVER)),
                        'synthetic sources idle after PulseAudio clients and supervisor stop', seconds=12)
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
        data = session.dump()
        capture_id = nodes(data, session.runtime.CAPTURE)[0]['id']
        link = next(o for o in data if o['type'].endswith(':Link')
                    and o['info']['input-node-id'] == capture_id)
        # Read metadata before freezing its owner. The production supervisor's
        # graph-only client does not bind metadata and remains responsive.
        session.manager.send_signal(signal.SIGSTOP)
        # pw-cli binds every metadata object before commands and would wait
        # for the frozen owner. pw-link binds only nodes/ports/links.
        session.run('pw-link', '-d', str(link['id']))
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
    for binary in ('pipewire', 'pipewire-pulse', 'pactl', 'parec', 'pw-dump', 'pw-cli',
                   'pw-link', 'pw-cat', 'wpctl', 'wireplumber', 'dbus-run-session'):
        if not shutil.which(binary):
            parser.error('Required dependency missing: ' + binary)
    assert args.plugin.is_file() and args.wireplumber_config.is_file()
    if not args.inside_private_dbus:
        return subprocess.call(['dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
                                *sys.argv[1:], '--inside-private-dbus'])
    root = args.output_directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reports = []
    for name in ('healthy', 'pulse-clients', 'source-loss', 'link-loss', 'child-exit', 'server-disconnect', 'hung-child'):
        session = Session(root / name, args.plugin.resolve(), args.wireplumber_config.resolve())
        try:
            session.boot()
            if name == 'healthy':
                result = healthy(session)
            elif name == 'pulse-clients':
                result = pulse_healthy(session)
            else:
                result = fault(session, name)
            reports.append({'case': name, 'status': 'passed', 'results': result})
            print('PASS:', name, flush=True)
        except Exception as error:
            reports.append({'case': name, 'status': 'failed', 'error': str(error)})
            for log in session.root.glob('*.log'):
                print(log.name + ':\n' + log.read_text(errors='replace')[-4000:], flush=True)
            for graph in session.root.glob('*timeout-graph.json'):
                for obj in json.loads(graph.read_text()):
                    if obj['type'].endswith(('Node', 'Metadata', 'Link')):
                        print(json.dumps(obj), flush=True)
            # Every case gets fresh private synthetic services and state.
            # Preserve independent fault evidence even if another case fails.
            traceback.print_exc()
        finally:
            session.close()
            (root / 'results.json').write_text(json.dumps(reports, indent=2))
    if any(report['status'] != 'passed' for report in reports):
        return 1
    print('PASS: real synthetic session and actual supervisor; no hardware or systemd-manager qualification')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
