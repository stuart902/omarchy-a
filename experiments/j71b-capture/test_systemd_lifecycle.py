#!/usr/bin/env python3
"""CI-only systemd user-manager lifecycle tests with non-audio fixtures.

Requires an existing manager on a disposable GitHub-hosted VM. Never starts a
manager, enables linger, uses sudo, or connects to a local developer session.
Production unit text is preserved in one inert, uniquely named runtime copy.
A second copy substitutes only fixture names, commands and hardware conditions.
Its lifecycle directives remain unchanged. The worker uses the production stop
and ownership helpers, not serve(): real PCM/supervisor coverage is separate.
This does not qualify the production board entry point, hardware or desktop UI.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / 'system/overlay/usr/lib/systemd/user/omarchy-j71b-microphone.service'
RUNTIME = ROOT / 'system/overlay/usr/lib/omarchy-j71b-microphone/desktop_mic.py'
HERE = Path(__file__).resolve()
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'Result', 'MainPID',
              'ExecMainStatus', 'NRestarts', 'ControlGroup', 'UnitFileState',
              'Requisite', 'After', 'Restart', 'KillMode', 'SendSIGKILL',
              'TimeoutStopUSec', 'Conditions', 'ExecMainStartTimestampMonotonic')
MODES = ('healthy', 'fault', 'hung-child', 'hung-main')


def load_runtime():
    spec = importlib.util.spec_from_file_location('j71b_systemd_runtime', RUNTIME)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime


def require_ci():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or os.geteuid() == 0):
        raise RuntimeError('Only the unprivileged disposable GitHub-hosted job is authorized')
    if Path('/dev/snd').exists() or Path('/dev/bus/usb').exists():
        raise RuntimeError('Refusing a runner with audio or USB device interfaces exposed')


def quote(value):
    """One systemd command argument, including literal dollar/specifier escapes."""
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('A unit argument cannot contain a newline')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def fixture_unit(original, dependencies, directory, mode):
    """Fail closed if production entries change rather than loosely removing gates."""
    expected = {'Description', 'Requisite', 'After', 'ConditionArchitecture',
                'ConditionPathExists', 'Type', 'ExecCondition', 'ExecStart',
                'Restart', 'KillMode', 'SendSIGKILL', 'TimeoutStopSec', 'UMask'}
    seen = set()
    for line in original.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line in ('[Unit]', '[Service]'):
            continue
        key, separator, _ = line.partition('=')
        if not separator or key not in expected or key in seen:
            raise ValueError('Production unit changed; review fixture directive: ' + line)
        seen.add(key)
    if seen != expected:
        raise ValueError('Production unit is missing expected fixture directives')
    substitutions = {
        'Description=J71b conditioned microphone (experimental, opt-in)':
            'Description=J71b CI-only synthetic lifecycle fixture',
        'Requisite=pipewire.service wireplumber.service': 'Requisite=' + ' '.join(dependencies),
        'After=pipewire.service wireplumber.service': 'After=' + ' '.join(dependencies),
        'ConditionArchitecture=arm64': '# CI fixture only: no hardware architecture gate',
        'ConditionPathExists=/proc/device-tree/compatible': '# CI fixture only: no device-tree gate',
        'ExecCondition=/usr/bin/python3 /usr/lib/omarchy-j71b-microphone/desktop_mic.py --check-platform':
            'ExecCondition=/usr/bin/true',
        'ExecStart=/usr/bin/python3 /usr/lib/omarchy-j71b-microphone/desktop_mic.py':
            'ExecStart=' + ' '.join(map(quote, (sys.executable, HERE, '--worker', mode,
                                               '--output-directory', directory))),
    }
    lines = original.splitlines()
    for line in substitutions:
        if lines.count(line) != 1:
            raise ValueError('Production unit changed; review fixture substitution: ' + line)
    result = '\n'.join(substitutions.get(line, line) for line in lines) + '\n'
    result += ('\n[Service]\nEnvironment=GITHUB_ACTIONS=true RUNNER_ENVIRONMENT=github-hosted\n'
               'StandardOutput=append:' + str(directory / 'service.log') + '\n'
               'StandardError=append:' + str(directory / 'service.log') + '\n')
    return result


def record(directory, event, **data):
    with (directory / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps({'event': event, 'pid': os.getpid(), **data}) + '\n')


def child(directory, mode):
    require_ci()
    done = False

    def received(signum, unused):
        nonlocal done
        record(directory, 'child-signal', signal=signal.Signals(signum).name)
        if mode != 'hung-child':
            done = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, received)
    (directory / 'child.pid').write_text(str(os.getpid()))
    # A CI-only watchdog bounds even orphaned fixture processes.
    deadline = time.monotonic() + 150
    while not done and time.monotonic() < deadline:
        time.sleep(.05)
    return 0


def worker(directory, mode):
    require_ci()
    runtime = load_runtime()
    runtime.STOP = False

    def received(signum, frame):
        record(directory, 'main-signal', signal=signal.Signals(signum).name)
        runtime.request_stop(signum, frame)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, received)
    with runtime.OwnershipGuard(directory / 'state', 'synthetic-ci-boot') as guard:
        guard.check()
        guard.mark()
        record(directory, 'main-start')
        process = subprocess.Popen([sys.executable, str(HERE), '--child', mode,
                                    '--output-directory', str(directory)])
        deadline = time.monotonic() + 120
        while not (directory / 'child.pid').exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('Synthetic child failed to become ready')
            time.sleep(.05)
        (directory / 'ready').touch()
        if mode == 'hung-main':
            # Deliberately ignore STOP in this one fixture, so the manager's
            # full stop and final-stop timers run with a live main process.
            while time.monotonic() < deadline:
                time.sleep(.05)
            raise RuntimeError('Synthetic hung-main watchdog expired')
        while not runtime.STOP and not (directory / 'inject-fault').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Synthetic worker watchdog expired')
            time.sleep(.05)
        stopped = runtime.stop_filter(process)
        if (directory / 'inject-fault').exists():
            raise RuntimeError('Injected fixture failure; ownership retained')
        if not stopped:
            raise RuntimeError('Synthetic child exited before stop')
        guard.clean()
        record(directory, 'clean-stop')
    return 0


def wait_for(predicate, description, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('Timeout: ' + description)


def process_state(pid):
    try:
        return next(line.split()[1] for line in Path(f'/proc/{pid}/status').read_text().splitlines()
                    if line.startswith('State:'))
    except FileNotFoundError:
        return None


class Manager:
    def __init__(self, directory):
        self.directory = directory
        runtime = Path('/run/user') / str(os.getuid())
        self.unit_directory = runtime / 'systemd/user'
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8',
                    'XDG_RUNTIME_DIR': str(runtime),
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(runtime / 'bus')}
        self.files, self.units = [], []
        private = runtime / 'systemd/private'
        if (not private.exists() or not stat.S_ISSOCK(private.stat().st_mode) or
                private.stat().st_uid != os.getuid()):
            raise RuntimeError('Existing unprivileged systemd user manager unavailable: ' + str(private) +
                               '. No manager startup, sudo, linger, or privilege workaround attempted.')
        version = self.run('show', '--property=Version', '--value')
        (directory / 'manager-version.txt').write_text(version.stdout)

    def run(self, *args, check=True, timeout=55):
        result = subprocess.run(['systemctl', '--user', '--no-pager', *args], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        with (self.directory / 'commands.log').open('a') as log:
            log.write(json.dumps({'args': args, 'returncode': result.returncode,
                                  'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
        if check and result.returncode:
            raise AssertionError('systemctl ' + ' '.join(args) + ': ' + result.stderr)
        return result

    def add(self, name, content):
        self.unit_directory.mkdir(parents=True, exist_ok=True)
        path = self.unit_directory / name
        with path.open('x') as stream:
            stream.write(content)
        self.files.append(path)
        self.units.append(name)
        (self.directory / name).write_text(content)

    def show(self, name):
        text = self.run('show', name, '--property=' + ','.join(PROPERTIES)).stdout
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def close(self):
        # Fixture-only cleanup AFTER observations. Never use a wildcard, touch
        # real audio services, or change production kill/restart policy.
        for unit in reversed(self.units):
            self.run('kill', '--kill-who=all', '--signal=KILL', unit, check=False)
            self.run('stop', unit, check=False)
            self.run('reset-failed', unit, check=False)
        for path in self.files:
            path.unlink()
        if self.files:
            self.run('daemon-reload')


def assert_lifecycle(properties):
    for key, value in {'LoadState': 'loaded', 'Restart': 'no', 'KillMode': 'mixed',
                       'SendSIGKILL': 'no', 'TimeoutStopUSec': '20s',
                       'UnitFileState': 'static', 'NRestarts': '0'}.items():
        assert properties[key] == value, (key, properties)


def read_events(directory):
    return [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]


def run_case(manager, directory, name, mode):
    manager.run('start', name)
    wait_for(lambda: (directory / 'ready').exists(), mode + ' fixture ready')
    before = manager.show(name)
    assert_lifecycle(before)
    assert before['ActiveState'] == 'active', before
    pid = int((directory / 'child.pid').read_text())
    group = before['ControlGroup']
    assert group and group in Path(f'/proc/{pid}/cgroup').read_text(), 'Child escaped service cgroup'
    started = time.monotonic()
    if mode == 'fault':
        (directory / 'inject-fault').touch()
        wait_for(lambda: manager.show(name)['ActiveState'] == 'failed', 'failed unit')
    else:
        result = manager.run('stop', name, check=False)
        if mode == 'healthy':
            assert result.returncode == 0, result.stderr
    elapsed = time.monotonic() - started
    after = manager.show(name)
    assert_lifecycle(after)
    marker = directory / 'state/ownership.json'
    if mode == 'healthy':
        assert after['ActiveState'] == 'inactive' and after['Result'] == 'success', after
        assert not marker.exists() and process_state(pid) is None
    elif mode == 'fault':
        assert after['Result'] == 'exit-code' and after['ExecMainStatus'] != '0', after
        assert marker.is_file() and process_state(pid) is None
    elif mode == 'hung-child':
        # The real stop helper exits with failure after five seconds. systemd
        # may complete stop then, without waiting out its twenty-second timer.
        assert after['ActiveState'] == 'failed' and after['Result'] == 'exit-code', after
        assert after['ExecMainStatus'] != '0', after
        while time.monotonic() - started < 21:
            assert process_state(pid) not in (None, 'Z', 'X'), 'Hung child was killed'
            time.sleep(.1)
        assert marker.is_file() and process_state(pid) not in (None, 'Z', 'X')
        assert group in Path(f'/proc/{pid}/cgroup').read_text(), 'Hung child escaped cgroup'
    else:
        assert after['ActiveState'] == 'failed' and after['Result'] == 'timeout', after
        assert elapsed >= 19, ('Production 20-second stop timeout was shortened', elapsed)
        assert marker.is_file() and process_state(pid) not in (None, 'Z', 'X'), 'Hung child was killed'
        assert process_state(int(before['MainPID'])) not in (None, 'Z', 'X'), 'Hung main was killed'
        assert group in Path(f'/proc/{pid}/cgroup').read_text(), 'Hung child escaped cgroup'
    events = read_events(directory)
    child_signals = [event['signal'] for event in events if event['event'] == 'child-signal']
    expected_signals = [] if mode == 'hung-main' else ['SIGINT']
    assert child_signals == expected_signals, ('Manager signalled child directly', child_signals)
    main_signals = [event['signal'] for event in events if event['event'] == 'main-signal']
    if mode == 'hung-main':
        assert main_signals and set(main_signals) == {'SIGTERM'}, main_signals
    elif mode != 'fault':
        assert main_signals == ['SIGTERM'], main_signals
    # No automatic restart after success, explicit stop, or unexpected failure.
    time.sleep(2)
    final = manager.show(name)
    assert final['NRestarts'] == '0' and final['ExecMainStartTimestampMonotonic'] == before['ExecMainStartTimestampMonotonic']
    assert sum(event['event'] == 'main-start' for event in read_events(directory)) == 1
    if mode == 'hung-main':
        runtime = load_runtime()
        try:
            with runtime.OwnershipGuard(directory / 'state', 'synthetic-ci-boot'):
                raise AssertionError('Hung main unexpectedly released ownership lock')
        except RuntimeError as error:
            assert 'already active' in str(error)
    elif mode != 'healthy':
        runtime = load_runtime()
        with runtime.OwnershipGuard(directory / 'state', 'synthetic-ci-boot') as guard:
            try:
                guard.check()
            except RuntimeError as error:
                assert 'Previous microphone ownership was not cleared' in str(error)
            else:
                raise AssertionError('Ownership retry was not blocked')
    return {'case': mode, 'status': 'passed', 'before': before, 'after': after,
            'final': final, 'stop_elapsed_seconds': elapsed,
            'observation_seconds': time.monotonic() - started,
            'child_signals': child_signals, 'main_signals': main_signals}


def run_suite(directory):
    require_ci()
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve()
    directory = directory.resolve()
    if not directory.is_relative_to(runner_temp) or directory == runner_temp:
        raise RuntimeError('Diagnostics must be in a new child directory of RUNNER_TEMP')
    directory.mkdir(mode=0o700)
    original = UNIT.read_text()
    digest = hashlib.sha256(UNIT.read_bytes()).hexdigest()
    reports, manager = [], None
    try:
        manager = Manager(directory)
        prefix = 'j71b-ci-' + uuid.uuid4().hex
        dependencies = [prefix + '-' + suffix + '.service' for suffix in ('pipewire', 'wireplumber')]
        production = prefix + '-production.service'
        manager.add(production, original)
        for dependency in dependencies:
            # These are inert process fixtures, not actual audio services.
            manager.add(dependency, '[Service]\nType=simple\nExecStart=/usr/bin/sleep 150\nRestart=no\n')
        names = {}
        for mode in MODES:
            location = directory / mode
            location.mkdir(mode=0o700)
            names[mode] = prefix + '-' + mode + '.service'
            manager.add(names[mode], fixture_unit(original, dependencies, location, mode))
        manager.run('daemon-reload')
        for name in names.values():
            loaded = manager.show(name)
            assert_lifecycle(loaded)
            assert set(loaded['Requisite'].split()) == set(dependencies), loaded
            assert set(dependencies).issubset(loaded['After'].split()), loaded
        # The installed runtime copy is byte-for-byte production text, retains
        # its hardware gate and remains static/inactive. Never start it.
        installed = manager.show(production)
        assert_lifecycle(installed)
        assert installed['ActiveState'] == 'inactive', installed
        assert manager.run('is-enabled', production, check=False).stdout.strip() == 'static'
        assert (manager.unit_directory / production).read_text() == original
        assert 'ConditionArchitecture=arm64' in original
        assert 'ConditionPathExists=/proc/device-tree/compatible' in original
        assert 'desktop_mic.py --check-platform' in original and '\n[Install]' not in original
        for unit in [production, *names.values()]:
            assert not list(manager.unit_directory.glob('*.wants/' + unit)), 'Fixture was enabled'
        reports.append({'case': 'production-unit-opt-in', 'status': 'passed',
                        'sha256': digest, 'installed': installed})
        # Both inactive, then only one active: Requisite must never pull in the
        # other dependency or execute the worker. After makes this deterministic.
        for active_count in (0, 1):
            if active_count:
                manager.run('start', dependencies[0])
            result = manager.run('start', names['healthy'], check=False)
            assert result.returncode != 0, 'Requisite accepted an inactive dependency'
            assert not (directory / 'healthy/events.jsonl').exists(), 'Worker ran without prerequisites'
            for index, dependency in enumerate(dependencies):
                expected = 'active' if index < active_count else 'inactive'
                assert manager.show(dependency)['ActiveState'] == expected, 'Requisite started a dependency'
            manager.run('reset-failed', names['healthy'], check=False)
        reports.append({'case': 'inactive-requisite-does-not-start', 'status': 'passed'})
        manager.run('start', *dependencies)
        for mode, name in names.items():
            report = run_case(manager, directory / mode, name, mode)
            reports.append(report)
            print('PASS:', mode, flush=True)
        assert manager.show(production)['ActiveState'] == 'inactive'
        assert hashlib.sha256(UNIT.read_bytes()).hexdigest() == digest, 'Production unit changed'
    except Exception as error:
        reports.append({'case': 'suite', 'status': 'failed', 'error': str(error)})
        traceback.print_exc()
    finally:
        if manager:
            try:
                manager.close()
            except Exception as error:
                reports.append({'case': 'cleanup', 'status': 'failed', 'error': str(error)})
                traceback.print_exc()
        (directory / 'results.json').write_text(json.dumps(reports, indent=2))
    if any(report['status'] != 'passed' for report in reports):
        return 1
    print('PASS: real systemd user-manager lifecycle; synthetic processes only, no board/PCM qualification')
    return 0


class FixtureTests(unittest.TestCase):
    """Pure construction checks; safe locally, no processes or sockets."""
    def test_substitutions_preserve_lifecycle_and_production(self):
        original = UNIT.read_text()
        result = fixture_unit(original, ['test-a.service', 'test-b.service'], Path('/tmp/fixture'), 'healthy')
        for entry in ('Type=simple', 'Restart=no', 'KillMode=mixed', 'SendSIGKILL=no',
                      'TimeoutStopSec=20s', 'UMask=0077'):
            self.assertIn('\n' + entry + '\n', result)
        self.assertNotIn('\n[Install]', result)
        self.assertNotIn('ConditionArchitecture=', result)
        self.assertNotIn('ConditionPathExists=', result)
        self.assertEqual(UNIT.read_text(), original)

    def test_changed_production_is_not_silently_weakened(self):
        original = UNIT.read_text()
        for changed in (original.replace('ConditionArchitecture=arm64', 'ConditionArchitecture=arm'),
                        original + '\n[Unit]\nWants=pipewire.service\n',
                        original + '\n[Service]\nExecStartPost=/usr/bin/false\n',
                        original + '\n[Install]\nWantedBy=default.target\n'):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                fixture_unit(changed, ['a.service', 'b.service'], Path('/tmp/fixture'), 'healthy')

    def test_command_arguments_are_literal(self):
        self.assertEqual(quote('some path/$arg%value"'), '"some path/$$arg%%value\\""')
        with self.assertRaises(ValueError):
            quote('newline\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--run-existing-user-manager', action='store_true')
    parser.add_argument('--output-directory', type=Path)
    parser.add_argument('--worker', choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument('--child', choices=MODES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        return not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FixtureTests)).wasSuccessful()
    if args.output_directory is None:
        parser.error('--output-directory is required')
    if args.worker:
        return worker(args.output_directory, args.worker)
    if args.child:
        return child(args.output_directory, args.child)
    if not args.run_existing_user_manager:
        parser.error('Explicit CI user-manager opt-in is required')
    return run_suite(args.output_directory)


if __name__ == '__main__':
    raise SystemExit(main())
