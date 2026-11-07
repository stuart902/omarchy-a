"""Opt-in, one-recording J71b test using the existing desktop audio server.

No installation, default changes, playback, driver operations, or retry loop.
This is a hardware acceptance test, not a production integration.
"""
import argparse
from array import array
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import wave

SOURCE = 'alsa_input.platform-microphones.stereo-fallback'
CAPTURE = 'capture.j71b.acceptance'
OUTPUT = 'j71b.acceptance'
RECORDER = 'record.j71b.acceptance'
ENV = None
FAULT = re.compile(
    r'retaining resources until device reset|'
    r'(?:pdm|smartio|apple-sio|aop).*?(?:failed|fault|timeout|timed out)|'
    r'(?:BUG:|Oops:|kernel panic|DMA termination.*failed)', re.I)


def run(command, timeout=8, env=None):
    result = subprocess.run(command, text=True, capture_output=True,
                            timeout=timeout, env=env if env is not None else ENV)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed: {result.stderr.strip()}')
    return result.stdout


def clean_environment(environment):
    allowed = {'PIPEWIRE_REMOTE', 'PIPEWIRE_RUNTIME_DIR'}
    forbidden = sorted(key for key in environment if (
        key.startswith('PIPEWIRE_') and key not in allowed) or key.startswith('SPA_') or key in {
            'LADSPA_PATH', 'LD_LIBRARY_PATH', 'LD_PRELOAD'})
    if forbidden:
        raise RuntimeError('Audio/loader environment overrides must be removed for this test: ' + ', '.join(forbidden))
    if environment.get('PIPEWIRE_REMOTE', 'pipewire-0') != 'pipewire-0':
        raise RuntimeError('Only the existing default desktop server pipewire-0 is supported')
    result = dict(environment)
    result['PIPEWIRE_DAEMON'] = 'false'
    return result


def isolate_client_environment(environment, directory):
    # An absolute config prefix confines both the main config and .conf.d
    # lookup. Existing user/system client.conf stream rules cannot retarget us.
    directory = directory.resolve()
    client = {
        'context.properties': {'core.daemon': False},
        'context.spa-libs': {'audio.convert.*': 'audioconvert/libspa-audioconvert',
                             'support.*': 'support/libspa-support'},
        'context.modules': [{'name': 'libpipewire-module-' + name}
                            for name in ('protocol-native', 'client-node', 'adapter')],
    }
    (directory/'client.conf').write_text(json.dumps(client))
    result = dict(environment)
    result.update(PIPEWIRE_DAEMON='false', PIPEWIRE_CONFIG_PREFIX=str(directory),
                  PIPEWIRE_CONFIG_NAME='client.conf', PIPEWIRE_CONFIG_DIR=str(directory))
    return result


def dump():
    return json.loads(run(['pw-dump']))


def props(obj):
    return obj.get('info', {}).get('props', obj.get('props', {}))


def nodes(data):
    return [o for o in data if o.get('type') == 'PipeWire:Interface:Node']


def node(data, name):
    found = [o for o in nodes(data) if props(o).get('node.name') == name]
    if len(found) != 1:
        raise RuntimeError(f'Expected exactly one node named {name}; found {len(found)}')
    return found[0]


def serial(obj):
    value = str(props(obj).get('object.serial', ''))
    if not value.isdecimal():
        raise RuntimeError('Missing numeric object.serial; refusing name-only target')
    return value


def defaults(data):
    return [item for obj in data if obj.get('type') == 'PipeWire:Interface:Metadata'
            for item in obj.get('metadata', [])
            if item.get('key') in ('default.audio.source', 'default.configured.audio.source')]


def preflight(data, compatible, machine):
    if machine != 'aarch64' or not {'apple,j71b', 'apple,t8010'} <= set(compatible):
        raise RuntimeError('This test is only for the J71b/T8010 iPad7,5 on AArch64')
    source = node(data, SOURCE)
    p = props(source)
    if p.get('media.class') != 'Audio/Source' or str(p.get('audio.channels')) != '2':
        raise RuntimeError('Expected the verified two-channel microphone source')
    serial(source)
    if source.get('info', {}).get('state') not in ('idle', 'suspended'):
        raise RuntimeError('Microphone is already active; close recording clients first')
    for obj in nodes(data):
        p = props(obj)
        if p.get('node.name') in (CAPTURE, OUTPUT, RECORDER):
            raise RuntimeError('A test node already exists; do not start a second session')
        if p.get('media.class') == 'Audio/Sink' and (
                p.get('device.api') == 'alsa' or
                str(p.get('node.name', '')).startswith('alsa_output.')):
            raise RuntimeError('An ALSA playback node is present; expected microphone-only image')
    return source


def check_kernel(text):
    entries = [line for line in text.splitlines()
               if line.strip() and not line.startswith('boot_id=')]
    if not entries or '-- No entries --' in text:
        raise RuntimeError('Kernel log is empty or inaccessible; no fault check was possible')
    match = FAULT.search(text)
    if match:
        raise RuntimeError(f'Kernel fault marker found: {match.group(0)}. Stop; do not retry capture')


def kernel_before(path):
    if path is None:
        result = subprocess.run(['journalctl', '-k', '-b', '--no-pager', '-o', 'cat'],
                                text=True, capture_output=True, timeout=8, env=ENV)
        if result.returncode or 'permission' in result.stderr.lower() or 'not seeing' in result.stderr.lower():
            raise RuntimeError('Kernel journal unavailable. Use the documented --kernel-log export; do not run this launcher as root')
        text = result.stdout
    else:
        path = path.resolve()
        if time.time() - path.stat().st_mtime > 120:
            raise RuntimeError('Kernel export is older than two minutes; create a fresh export')
        text = path.read_text()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not text.startswith('boot_id=' + boot + '\n'):
            raise RuntimeError('Kernel export does not identify this boot')
    check_kernel(text)
    return text


def check_versions(data, tool_version):
    cores = [o for o in data if o.get('type') == 'PipeWire:Interface:Core']
    if len(cores) != 1 or cores[0].get('info', {}).get('version') != '1.6.10':
        raise RuntimeError('The existing PipeWire server must be exactly version 1.6.10')
    if re.search(r'^Linked with libpipewire 1\.6\.10\s*$', tool_version, re.M) is None:
        raise RuntimeError('The PipeWire client must link exactly libpipewire 1.6.10')


def config(plugin, source_serial, gain):
    return {
        'context.properties': {'core.daemon': False, 'application.name': 'J71b microphone acceptance'},
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
                'node.name': CAPTURE, 'target.object': source_serial,
                'node.autoconnect': True, 'node.dont-fallback': True,
                'node.dont-reconnect': True, 'node.passive': True,
                'node.always-process': False, 'node.pause-on-idle': True,
            },
            'playback.props': {
                'node.name': OUTPUT, 'node.description': 'J71b temporary microphone test',
                'media.class': 'Audio/Source', 'node.virtual': True,
                'node.autoconnect': False, 'node.dont-reconnect': True,
                'node.always-process': False, 'node.pause-on-idle': True,
                'priority.session': -10000, 'priority.driver': 0,
            },
        }}],
    }


def check_routes(data, source_serial, original_defaults, recording=False,
                 draining_recorder_id=None):
    source = node(data, SOURCE)
    capture, output = node(data, CAPTURE), node(data, OUTPUT)
    if serial(source) != source_serial:
        raise RuntimeError('Microphone identity changed; no reconnection is permitted')
    if defaults(data) != original_defaults:
        raise RuntimeError('Default microphone changed during test; stop and inspect')
    capture_id, output_id = capture['id'], output['id']
    recorder = [o for o in nodes(data) if props(o).get('node.name') == RECORDER]
    if len(recorder) > 1:
        raise RuntimeError('Multiple test recorder nodes appeared')
    recorder_id = recorder[0]['id'] if recorder else draining_recorder_id
    if draining_recorder_id is not None and recorder_id != draining_recorder_id:
        raise RuntimeError('Recorder identity changed during cleanup')
    inputs, outputs = [], []
    for link in data:
        if link.get('type') != 'PipeWire:Interface:Link':
            continue
        info = link.get('info', {})
        src, dst = info.get('output-node-id'), info.get('input-node-id')
        if src == source['id'] and dst != capture_id:
            raise RuntimeError('Another consumer connected to the raw microphone')
        if dst == capture_id:
            if src != source['id']:
                raise RuntimeError('Unexpected input connected to test filter')
            inputs.append(info)
        if src == output_id:
            if not recording or recorder_id is None or dst != recorder_id:
                raise RuntimeError('Unexpected consumer connected to test source')
            outputs.append(info)
        if recorder_id is not None and dst == recorder_id and src != output_id:
            raise RuntimeError('Recorder connected to a source other than the test filter')
        if src == capture_id or dst == output_id:
            raise RuntimeError('Unexpected link direction at test filter')
    if len(inputs) != 2:
        raise RuntimeError(f'Expected exactly two microphone-to-filter links; found {len(inputs)}')
    ports = {o['id']: props(o) for o in data if o.get('type') == 'PipeWire:Interface:Port'}
    for links in (inputs, outputs):
        channels = []
        for info in links:
            if info.get('state') == 'error':
                raise RuntimeError('A test link entered the error state')
            source_channel = ports.get(info.get('output-port-id'), {}).get('audio.channel')
            target_channel = ports.get(info.get('input-port-id'), {}).get('audio.channel')
            if source_channel not in ('FL', 'FR') or source_channel != target_channel:
                raise RuntimeError('Unverified or crossed stereo channel mapping')
            channels.append(source_channel)
        if len(links) == 2 and set(channels) != {'FL', 'FR'}:
            raise RuntimeError('Duplicate stereo channel mapping')
        if len(links) > 2:
            raise RuntimeError('Too many test channel links')
    return output, source, len(outputs)


def recording_stats(path):
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) != (2, 2, 48000, 480000):
            raise RuntimeError('Recording does not contain the expected ten seconds of stereo S16/48k PCM')
        payload = wav.readframes(480000)
        if len(payload) != 480000 * 2 * 2:
            raise RuntimeError('Recording payload is truncated despite its WAV header')
        samples = array('h', payload)
    if sys.byteorder != 'little':
        samples.byteswap()
    return {'frames': 480000, 'rate': 48000, 'channels': [
        {'mean_sample': sum(samples[ch::2]) / 480000,
         'peak_sample': max(abs(v) for v in samples[ch::2]),
         'samples_near_emergency_bound': sum(abs(v) >= 29203 for v in samples[ch::2])}
        for ch in range(2)]}


def validate_recorder_completion(path, returncode, seen_flow, recorder_log):
    # In pinned PipeWire 1.6.10, pw-cat's recording sample-limit path quits
    # without setting data.drained. main therefore returns EXIT_FAILURE (1)
    # even after writing the requested frames. Never treat that status alone
    # as success: require the observed stereo route, silent diagnostics, and
    # the exact WAV header AND physical payload. Lifecycle checks still follow.
    if returncode not in (0, 1):
        raise RuntimeError(f'Recorder exited with status {returncode}; inspect recorder.log')
    if not seen_flow:
        raise RuntimeError('Two-channel recording route was not observed')
    if recorder_log.strip():
        raise RuntimeError('Recorder emitted diagnostics; inspect recorder.log before another capture')
    return recording_stats(path)


def stop_child(child, label):
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{label} did not stop. Do not retry, unload drivers, or reboot in software; a physical reset may be required')


def main(client_directory):
    global ENV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly run one ten-second capture; otherwise inspect only')
    parser.add_argument('--gain-db', type=int, choices=(0, 12, 24), default=0)
    parser.add_argument('--kernel-log', type=Path)
    parser.add_argument('--output', type=Path, default=Path('j71b-conditioned.wav'))
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError('Run as the desktop user, not root')
    ENV = clean_environment(os.environ)
    runtime = Path(ENV.get('XDG_RUNTIME_DIR', ''))
    if not runtime.is_absolute() or runtime.stat().st_uid != os.getuid():
        raise RuntimeError('A desktop-user XDG_RUNTIME_DIR is required')
    if Path(ENV.get('PIPEWIRE_RUNTIME_DIR', str(runtime))).resolve() != runtime.resolve():
        raise RuntimeError('PipeWire runtime directory differs from the desktop session')
    ENV = isolate_client_environment(ENV, client_directory)
    compatible = Path('/proc/device-tree/compatible').read_bytes().decode().rstrip('\0').split('\0')
    before = dump()
    source = preflight(before, compatible, platform.machine())
    version = run(['pipewire', '--version'])
    check_versions(before, version)
    kernel = kernel_before(args.kernel_log)
    plugin = Path(__file__).resolve().with_name('j71b-capture.so')
    if not plugin.is_file():
        raise RuntimeError('Use the complete native ARM test package; plugin is missing')
    run([str(plugin.with_name('test-capture'))])
    print(f'Preflight passed: J71b, source serial {serial(source)}, gain {args.gain_db} dB.', flush=True)
    if not args.run:
        print('Inspection only. No filter or recording started.')
        return
    output_file = args.output.resolve()
    if output_file.exists():
        raise RuntimeError('Output file already exists; choose a new name')
    session = Path(tempfile.mkdtemp(prefix='j71b-acceptance-', dir=str(output_file.parent)))
    os.chmod(session, 0o700)
    print(f'Test logs: {session}', flush=True)
    # Reserve the new WAV privately and exclusively before passing it to pw-record.
    fd = os.open(output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    (session/'before.json').write_text(json.dumps(before, indent=2))
    (session/'kernel-before.txt').write_text(kernel)
    conf = session/'j71b-acceptance.conf'
    conf.write_text(json.dumps(config(plugin, serial(source), args.gain_db), indent=2))
    env = ENV.copy()
    env['PIPEWIRE_CONFIG_DIR'] = str(session)
    env['PIPEWIRE_CONFIG_PREFIX'] = str(session)
    env['PIPEWIRE_CONFIG_NAME'] = conf.name
    child = recorder = None
    filter_log = open(session/'filter.log', 'w')
    recorder_log = open(session/'recorder.log', 'w')
    try:
        child = subprocess.Popen(['pipewire', '-c', conf.name], env=env,
                                 stdout=filter_log, stderr=filter_log)
        deadline = time.monotonic() + 8
        while True:
            if child.poll() is not None:
                raise RuntimeError('Test filter exited; inspect filter.log, do not retry blindly')
            current = dump()
            try:
                test_source, raw, _ = check_routes(current, serial(source), defaults(before))
                break
            except RuntimeError as error:
                # Only node/link creation may need time. Wrong routes are fatal.
                if not (str(error).startswith('Expected exactly one node') or
                        str(error).startswith('Expected exactly two microphone-to-filter')):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        if raw.get('info', {}).get('state') == 'running':
            raise RuntimeError('Microphone started before recording; idle lifecycle is not safe')
        print('Recording starts in three seconds. Speak normally for ten seconds.', flush=True)
        time.sleep(3)
        _, raw, _ = check_routes(dump(), serial(source), defaults(before))
        if raw.get('info', {}).get('state') == 'running':
            raise RuntimeError('Microphone became active before the controlled recording')
        command = ['pw-record', '--target=' + serial(test_source), '--rate=48000',
                   '--channels=2', '--channel-map=FL,FR', '--format=s16',
                   '--sample-count=480000', '--properties=' + json.dumps({
                       'node.name': RECORDER, 'node.dont-fallback': True,
                       'node.dont-reconnect': True}), str(output_file)]
        recorder = subprocess.Popen(command, stdout=recorder_log, stderr=recorder_log,
                                    env=ENV, umask=0o077)
        deadline = time.monotonic() + 20
        seen_flow = False
        recorder_id = None
        while recorder.poll() is None:
            if child.poll() is not None:
                raise RuntimeError('Filter stopped during recording')
            current = dump()
            _, _, links = check_routes(current, serial(source), defaults(before), recording=True)
            current_recorder = [o for o in nodes(current) if props(o).get('node.name') == RECORDER]
            if current_recorder:
                recorder_id = current_recorder[0]['id']
            if links == 2 and not seen_flow:
                (session/'recording.json').write_text(json.dumps(current, indent=2))
            seen_flow |= links == 2
            if time.monotonic() >= deadline:
                raise RuntimeError('Recording did not finish. Stop; do not retry capture')
            time.sleep(.2)
        (session/'last-record.json').write_text(json.dumps(current, indent=2))
        outcome = {'returncode': recorder.returncode, 'observed_stereo_route': seen_flow,
                   'recorder_node_id': recorder_id}
        (session/'recorder-outcome.json').write_text(json.dumps(outcome, indent=2))
        stats = validate_recorder_completion(output_file, recorder.returncode, seen_flow,
                                             (session/'recorder.log').read_text())
        (session/'recording-stats.json').write_text(json.dumps(stats, indent=2))
        if recorder.returncode == 1:
            print('Complete ten-second WAV and stereo route validated; PipeWire 1.6.10 returned status 1, also used for sample-limit completion.', flush=True)
        # Allow the normal session-manager idle timeout, without forcing suspend.
        deadline = time.monotonic() + 8
        while True:
            after = dump()
            _, raw, links = check_routes(after, serial(source), defaults(before),
                                         recording=True, draining_recorder_id=recorder_id)
            if links == 0 and raw.get('info', {}).get('state') in ('idle', 'suspended'):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Microphone stayed active after recording; lifecycle check failed')
            time.sleep(.2)
        (session/'after-record.json').write_text(json.dumps(after, indent=2))
        print('Recorder exited; microphone returned to idle. Stopping only the test filter.', flush=True)
    finally:
        try:
            stop_child(recorder, 'Recorder')
        finally:
            try:
                stop_child(child, 'Filter client')
            finally:
                filter_log.close()
                recorder_log.close()
    deadline = time.monotonic() + 5
    while True:
        final = dump()
        if defaults(final) != defaults(before) or serial(node(final, SOURCE)) != serial(source):
            raise RuntimeError('Default or microphone identity changed during cleanup')
        if not any(props(o).get('node.name') in (CAPTURE, OUTPUT, RECORDER) for o in nodes(final)):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Test nodes remain after cleanup; do not retry')
        time.sleep(.2)
    (session/'after-cleanup.json').write_text(json.dumps(final, indent=2))
    if args.kernel_log is None:
        (session/'kernel-after.txt').write_text(kernel_before(None))
    if any(ch['samples_near_emergency_bound'] for ch in stats['channels']):
        print('Warning: samples reached the emergency-bound region; do not increase gain.')
    print(f'Validated ten-second WAV: {output_file}. No playback was started. Check the fresh kernel log before another capture.')


if __name__ == '__main__':
    try:
        with tempfile.TemporaryDirectory(prefix='j71b-client-config-') as directory:
            main(Path(directory))
    except KeyboardInterrupt:
        raise SystemExit('Stopped by user. Check cleanup and kernel logs before another capture.')
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(f'STOP: {error}')
