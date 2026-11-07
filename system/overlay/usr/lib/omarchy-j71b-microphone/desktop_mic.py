#!/usr/bin/env python3
"""Selectable J71b microphone, opt-in pending device lifecycle qualification.

Creates one client-only filter, never records, changes defaults or starts a
server. A persistent ownership marker blocks retries after abnormal teardown.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import tempfile
import time

SOURCE = 'alsa_input.platform-microphones.stereo-fallback'
CAPTURE = 'capture.j71b.microphone'
OUTPUT = 'j71b.microphone'
VERSION = '1.6.10'
STOP = False


class PendingGraph(RuntimeError):
    """Only initial node/link creation may be retried without reopening capture."""


def supported_board(compatible, machine):
    return machine == 'aarch64' and {'apple,j71b', 'apple,t8010'} <= set(compatible)


def properties(obj):
    return obj.get('info', {}).get('props', obj.get('props', {}))


def named_node(data, name):
    found = [obj for obj in data if obj.get('type') == 'PipeWire:Interface:Node'
             and properties(obj).get('node.name') == name]
    if not found:
        raise PendingGraph('Missing node: ' + name)
    if len(found) != 1:
        raise RuntimeError('Ambiguous node: ' + name)
    obj = found[0]
    if obj.get('info', {}).get('state') == 'error' or obj.get('info', {}).get('error'):
        raise RuntimeError('Node error: ' + name)
    return obj


def serial(obj):
    value = str(properties(obj).get('object.serial', ''))
    if not value.isdecimal():
        raise RuntimeError('Node has no numeric object serial')
    return value


def raw_source(data):
    source = named_node(data, SOURCE)
    props = properties(source)
    if (props.get('media.class') != 'Audio/Source' or
            props.get('device.api') != 'alsa' or
            props.get('alsa.card_name') != 'J71b Microphones' or
            props.get('api.alsa.pcm.stream') != 'capture' or
            str(props.get('audio.channels')) != '2'):
        raise RuntimeError('Raw node is not the J71b stereo ALSA microphone')
    serial(source)
    return source


def check_version(data, tool_version):
    cores = [obj for obj in data if obj.get('type') == 'PipeWire:Interface:Core']
    if len(cores) != 1 or cores[0].get('info', {}).get('version') != VERSION:
        raise RuntimeError('This integration requires the tested PipeWire ' + VERSION)
    if 'Linked with libpipewire ' + VERSION not in tool_version.splitlines():
        raise RuntimeError('PipeWire client version differs from the tested runtime')


def filter_config(plugin, source_serial, gain=12):
    if gain not in (0, 12, 24):
        raise ValueError('Unsupported gain')
    if not str(source_serial).isdecimal():
        raise ValueError('A pinned source serial is required')
    return {
        'context.properties': {'core.daemon': False, 'application.name': 'J71b microphone'},
        'context.spa-libs': {'audio.convert.*': 'audioconvert/libspa-audioconvert',
                             'support.*': 'support/libspa-support'},
        'context.modules': [
            {'name': 'libpipewire-module-' + name}
            for name in ('protocol-native', 'client-node', 'adapter')
        ] + [{'name': 'libpipewire-module-filter-chain', 'args': {
            'audio.channels': 2, 'audio.position': ['FL', 'FR'], 'audio.rate': 48000,
            'filter.graph': {
                'nodes': [{'type': 'ladspa', 'name': 'capture', 'plugin': str(plugin),
                           'label': 'j71b_capture_experiment', 'control': {'Gain (dB)': gain}}],
                'inputs': ['capture:Input L', 'capture:Input R'],
                'outputs': ['capture:Output L', 'capture:Output R'],
            },
            'capture.props': {
                'node.name': CAPTURE, 'target.object': str(source_serial),
                'node.autoconnect': True, 'node.dont-fallback': True,
                'node.dont-reconnect': True, 'node.passive': True,
                'node.always-process': False, 'node.pause-on-idle': True,
                'stream.dont-remix': True,
            },
            'playback.props': {
                'node.name': OUTPUT, 'node.description': 'J71b Microphone (conditioned)',
                'media.class': 'Audio/Source', 'node.virtual': True,
                'node.autoconnect': False, 'node.dont-reconnect': True,
                'node.always-process': False, 'node.pause-on-idle': True,
                # Driver priority is unsigned in PipeWire. Zero is valid.
                # Lower than the raw source; do not replace the user's default.
                'priority.session': 0, 'priority.driver': 0,
            },
        }}],
    }


def validate_graph(data, source_serial, require_links=True):
    """Check our input strictly; permit ordinary mono/stereo/multi-client output.

Raw ALSA remains available to other clients. Default metadata is never written
or policed: a user's deliberate selection must remain possible.
"""
    raw = raw_source(data)
    if serial(raw) != source_serial:
        raise RuntimeError('Raw microphone identity changed; refusing reconnection')
    capture, output = named_node(data, CAPTURE), named_node(data, OUTPUT)
    if properties(output).get('media.class') != 'Audio/Source':
        raise RuntimeError('Conditioned node is not a microphone source')
    inputs, consumers, other_raw = [], 0, 0
    for obj in data:
        if obj.get('type') != 'PipeWire:Interface:Link':
            continue
        info = obj.get('info', {})
        src, dst = info.get('output-node-id'), info.get('input-node-id')
        ours = dst == capture['id'] or src == output['id']
        if ours and (info.get('state') == 'error' or info.get('error')):
            raise RuntimeError('Microphone link error')
        if src == capture['id'] or dst == output['id']:
            raise RuntimeError('Unexpected filter link direction')
        if dst == capture['id']:
            if src != raw['id']:
                raise RuntimeError('Filter input connected to the wrong source')
            inputs.append(info)
        if src == output['id']:
            consumers += 1
        if src == raw['id'] and dst != capture['id']:
            other_raw += 1
    if len(inputs) != 2:
        if len(inputs) < 2 and not require_links:
            raise PendingGraph('Waiting for both microphone channels')
        raise RuntimeError('Lost or duplicate raw microphone links')
    ports = {obj['id']: properties(obj) for obj in data
             if obj.get('type') == 'PipeWire:Interface:Port'}
    channels = []
    for link in inputs:
        src = ports.get(link.get('output-port-id'), {}).get('audio.channel')
        dst = ports.get(link.get('input-port-id'), {}).get('audio.channel')
        if src not in ('FL', 'FR') or dst != src:
            raise RuntimeError('Crossed or unknown raw channel mapping')
        channels.append(src)
    if set(channels) != {'FL', 'FR'}:
        raise RuntimeError('Duplicate raw channel mapping')
    return raw, output, consumers, other_raw


def private_environment(environment, directory):
    allowed = {'PIPEWIRE_REMOTE', 'PIPEWIRE_RUNTIME_DIR'}
    forbidden = [key for key in environment if (
        key.startswith('PIPEWIRE_') and key not in allowed) or key.startswith('SPA_')
        or key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'LADSPA_PATH')]
    if forbidden or environment.get('PIPEWIRE_REMOTE', 'pipewire-0') != 'pipewire-0':
        raise RuntimeError('Remove PipeWire/loader overrides before starting the microphone')
    client = filter_config(Path('/unused'), '0')
    client['context.modules'] = client['context.modules'][:3]
    (directory / 'client.conf').write_text(json.dumps(client))
    return {**environment, 'PIPEWIRE_DAEMON': 'false',
            'PIPEWIRE_CONFIG_PREFIX': str(directory), 'PIPEWIRE_CONFIG_DIR': str(directory),
            'PIPEWIRE_CONFIG_NAME': 'client.conf'}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(directory):
    missing, current = [], directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (directory.is_symlink() or directory.stat().st_uid != os.getuid() or
            directory.stat().st_mode & 0o022):
        raise RuntimeError('Unsafe microphone state directory')
    # Make each newly created path entry durable before storing ownership.
    for created in reversed(missing):
        sync_directory(created)
        sync_directory(created.parent)


class OwnershipGuard:
    """Persistent across user-session recreation; no automatic recovery after fault."""
    def __init__(self, directory, boot):
        durable_directory(directory)
        self.marker = directory / 'ownership.json'
        self.boot = boot
        fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'w')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('The J71b microphone service is already active') from None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.lock.close()

    def check(self):
        if self.marker.exists() or self.marker.is_symlink():
            raise RuntimeError('Previous microphone ownership was not cleared. Stop; inspect logs and '
                               'perform a physical reset before using --reset-after-physical-reset')

    def mark(self):
        fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'boot_id': self.boot, 'status': 'ownership not yet cleared'}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.marker.parent)

    def clean(self):
        self.marker.unlink()
        sync_directory(self.marker.parent)

    def reset_after_physical_reset(self):
        if not self.marker.exists() or self.marker.is_symlink():
            raise RuntimeError('No valid ownership marker to reset')
        record = json.loads(self.marker.read_text())
        if not record.get('boot_id') or record['boot_id'] == self.boot:
            raise RuntimeError('Refusing recovery in the same boot')
        self.marker.unlink()
        sync_directory(self.marker.parent)


def command(argv, env):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError(Path(argv[0]).name + ' failed: ' + result.stderr.strip())
    return result.stdout


def snapshot(env):
    return json.loads(command(['/usr/bin/pw-dump'], env))


def stop_filter(child):
    # False means it exited before we asked, so cleanup must not clear the
    # ownership marker even when a simultaneous service stop was requested.
    if child is None or child.poll() is not None:
        return False
    child.send_signal(signal.SIGINT)
    try:
        status = child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Filter did not exit. Do not retry or unload drivers; physical reset may be needed') from None
    if status not in (0, -signal.SIGINT):
        raise RuntimeError('Filter failed during shutdown; ownership marker retained')
    return True


def cleanup_verified(data, source_serial):
    raw = raw_source(data)
    if serial(raw) != source_serial:
        raise RuntimeError('Raw source changed during cleanup')
    if any(properties(obj).get('node.name') in (CAPTURE, OUTPUT)
           for obj in data if obj.get('type') == 'PipeWire:Interface:Node'):
        return False
    # Require idle before allowing another service start. Close direct raw
    # recorders too when stopping this service, rather than guessing ownership.
    return raw.get('info', {}).get('state') in ('idle', 'suspended')


def serve(env, directory, plugin, guard):
    data = snapshot(env)
    raw = raw_source(data)
    check_version(data, command(['/usr/bin/pipewire', '--version'], env))
    if raw.get('info', {}).get('state') not in ('idle', 'suspended'):
        raise RuntimeError('Close recording apps before starting this service')
    for obj in data:
        if properties(obj).get('node.name') in (CAPTURE, OUTPUT):
            raise RuntimeError('A conditioned microphone already exists')
    source_serial = serial(raw)
    (directory / 'filter.conf').write_text(json.dumps(filter_config(plugin, source_serial)))
    guard.mark()  # Crash or SIGKILL leaves this in place, including across login.
    child = None
    healthy_stop = False
    try:
        child = subprocess.Popen(['/usr/bin/pipewire', '-c', 'filter.conf'],
                                 env={**env, 'PIPEWIRE_CONFIG_NAME': 'filter.conf'})
        deadline, ready, idle_since = time.monotonic() + 8, False, None
        while not STOP:
            if child.poll() is not None:
                raise RuntimeError('Microphone filter exited unexpectedly')
            data = snapshot(env)  # Disconnect/errors fail; never open a new server.
            # Once a source identity is selected, its loss is never a startup race.
            current_raw = raw_source(data)
            if serial(current_raw) != source_serial:
                raise RuntimeError('Microphone identity changed')
            try:
                current_raw, output, consumers, other_raw = validate_graph(
                    data, source_serial, require_links=ready)
            except PendingGraph:
                if ready or time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
                continue
            ready = True
            if not consumers and not other_raw and current_raw.get('info', {}).get('state') == 'running':
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= 10:
                    raise RuntimeError('Microphone remained active without a recording client')
            else:
                idle_since = None
            time.sleep(.5)
        healthy_stop = True
    finally:
        requested_exit = stop_filter(child)
        if healthy_stop and not requested_exit:
            raise RuntimeError('Filter exited before requested shutdown; ownership marker retained')
        if healthy_stop:
            deadline = time.monotonic() + 10
            while not cleanup_verified(snapshot(env), source_serial):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Microphone cleanup/idle not observed; ownership marker retained')
                time.sleep(.2)
            guard.clean()


def request_stop(signum, frame):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-platform', action='store_true')
    parser.add_argument('--reset-after-physical-reset', action='store_true',
                        help='Acknowledge a completed physical reset; never allowed in the faulted boot')
    args = parser.parse_args()
    try:
        compatible = Path('/proc/device-tree/compatible').read_bytes().decode().rstrip('\0').split('\0')
    except FileNotFoundError:
        compatible = []
    if not supported_board(compatible, platform.machine()):
        return 1 if args.check_platform else 0
    if args.check_platform:
        return 0
    if os.geteuid() == 0:
        raise RuntimeError('Use the desktop user service, never root')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    if not state.is_absolute():
        raise RuntimeError('XDG_STATE_HOME must be absolute')
    with OwnershipGuard(state / 'omarchy-j71b-microphone', boot) as guard:
        if args.reset_after_physical_reset:
            guard.reset_after_physical_reset()
            print('Ownership marker cleared after acknowledged physical reset. No microphone started.')
            return 0
        guard.check()
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR', ''))
        socket = runtime / 'pipewire-0'
        if (not runtime.is_absolute() or runtime.stat().st_uid != os.getuid() or
                not stat.S_ISSOCK(socket.stat().st_mode) or socket.stat().st_uid != os.getuid() or
                Path(os.environ.get('PIPEWIRE_RUNTIME_DIR', str(runtime))).resolve() != runtime.resolve()):
            raise RuntimeError('Existing desktop-user PipeWire socket required')
        plugin = Path(__file__).resolve().with_name('j71b-capture.so')
        if not plugin.is_file():
            raise RuntimeError('Installed microphone plugin is missing')
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, request_stop)
        with tempfile.TemporaryDirectory(prefix='j71b-microphone-', dir=runtime) as temp:
            directory = Path(temp)
            env = private_environment(os.environ, directory)
            serve(env, directory, plugin, guard)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit('J71b microphone stopped: ' + str(error))
