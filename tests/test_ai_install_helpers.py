"""Exercise the shipped AI menu actions without a compositor or package changes."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
HELPERS = (
    'omarchy-launch-floating-terminal-with-presentation',
    'omarchy-install-app',
    'omarchy-install-and-launch',
    'omarchy-pkg-add',
)
PRESENT = HELPERS[0]
PYTHON = Path('/usr/bin/python3')

spec = importlib.util.spec_from_file_location('ai_desktop_stage', PROJECT / 'desktop/stage.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


STUB_PRELUDE = '''import json, os, subprocess, sys
from pathlib import Path
def event(name, **values):
    with open(os.environ['TEST_EVENTS'], 'a') as stream:
        stream.write(json.dumps(dict(event=name, **values)) + '\\n')
args = sys.argv[1:]
'''


@unittest.skipUnless(PYTHON.exists() and shutil.which('setsid'),
                     'needs the Linux terminal and session tools used by the image')
class AIInstallHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        root = Path(cls.fixture.name)
        upstream = root / 'upstream'
        theme = upstream / 'themes/osaka-jade'
        (theme / 'backgrounds').mkdir(parents=True)
        (theme / 'colors.toml').write_text('foreground = "#ffffff"\n')
        (theme / 'backgrounds/first.webp').write_bytes(b'fallback')
        (upstream / 'applications').mkdir()
        (upstream / 'applications/foot.desktop').write_text('[Desktop Entry]\n')
        keyboard, xpi = root / 'keyboard', root / 'extension.xpi'
        keyboard.write_bytes(b'keyboard')
        xpi.write_bytes(b'extension')
        cls.payload = root / 'payload'
        args = ['stage.py', str(cls.payload), '--upstream', str(upstream),
                '--keyboard', str(keyboard), '--keyboard-sha256',
                hashlib.sha256(keyboard.read_bytes()).hexdigest(), '--omafox-xpi', str(xpi)]
        with patch.object(sys, 'argv', args), \
                patch.object(stage, 'XPI_SHA', hashlib.sha256(xpi.read_bytes()).hexdigest()), \
                contextlib.redirect_stdout(io.StringIO()):
            stage.main()
        cls.bin = cls.payload / 'home/tablet/.local/share/omarchy-ipad/bin'
        menu = cls.payload / 'home/tablet/.local/share/omarchy-ipad/default/omarchy/omarchy-menu.jsonc'
        cls.rows = {}
        for line in menu.read_text().splitlines():
            # Each shipped AI row is one JSON object; avoid a second JSONC parser.
            if re.match(r'\s*"install\.ai\.[^"]+"\s*:', line):
                cls.rows.update(json.loads('{' + line.strip().rstrip(',') + '}'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stubs = self.root / 'stubs'
        self.stubs.mkdir()
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        # No host PATH: a globally installed Omarchy helper must not hide an
        # omission from the staged image. External boundaries are stubs below.
        for name in ('bash', 'sh', 'setsid', 'mktemp', 'mkdir', 'chmod', 'mv',
                     'cat', 'rm', 'tee', 'date', 'sleep', 'dirname', 'touch'):
            executable = shutil.which(name)
            if executable:
                (self.tools / name).symlink_to(executable)
        self.events_file = self.root / 'events.jsonl'
        self.db = self.root / 'packages.json'
        self.db.write_text('[]')
        (self.root / 'tmp').mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ('BASH_ENV', 'ENV') and not key.startswith('BASH_FUNC_')}
        self.env.update(HOME=str(self.root), XDG_STATE_HOME=str(self.root / 'state'),
                        XDG_RUNTIME_DIR=str(self.root), TMPDIR=str(self.root / 'tmp'),
                        OMARCHY_PATH=str(self.bin.parent), TEST_EVENTS=str(self.events_file),
                        TEST_DB=str(self.db),
                        PATH=os.pathsep.join(map(str, (self.stubs, self.bin, self.tools))))
        self.stub('foot', '''event('foot', args=args)
if os.environ.get('FOOT_FAIL'):
    print('fixture: cannot connect to Wayland display', file=sys.stderr)
    sys.exit(int(os.environ['FOOT_FAIL']))
if os.environ.get('FOOT_SKIP_RUNNER'):
    sys.exit(0)
command = args[args.index('-e') + 1:]
status = subprocess.call(command, stderr=sys.stdout)
event('foot-child', status=status)
# Foot may report a successful window lifecycle after the child fails. The
# helper must carry the child's status separately rather than trust this 0.
sys.exit(0)
''')
        self.stub('sudo', '''event('sudo', args=args, tty=sys.stdin.isatty())
if os.environ.get('SUDO_REQUIRE_TTY') and not sys.stdin.isatty():
    print('fixture: sudo requires a terminal', file=sys.stderr)
    sys.exit(9)
if os.environ.get('SUDO_PROMPT'):
    print('[sudo] password for tablet: ', end='', flush=True)
    if not sys.stdin.readline().strip():
        sys.exit(1)
os.execvpe(args[0], args, os.environ)
''')
        self.stub('pacman', '''event('pacman', args=args)
db = Path(os.environ['TEST_DB'])
installed = set(json.loads(db.read_text()))
if args and args[0] == '-Q':
    packages = [p for p in args[1:] if p != '--']
    sys.exit(0 if all(p in installed for p in packages) else 1)
if args and args[0] == '-S':
    packages = args[args.index('--') + 1:]
    status = int(os.environ.get('PACMAN_INSTALL_EXIT', '0'))
    if status:
        print('error: target not found: ' + ' '.join(packages), file=sys.stderr)
        sys.exit(status)
    installed.update(p for p in packages if p != os.environ.get('PACMAN_VERIFY_MISSING'))
    db.write_text(json.dumps(sorted(installed)))
    sys.exit(0)
print('unexpected pacman arguments: ' + repr(args), file=sys.stderr)
sys.exit(90)
''')
        self.stub('gtk-launch', '''event('gtk-launch', args=args)
status = int(os.environ.get('LAUNCH_EXIT', '0'))
if status:
    print('fixture: desktop entry could not be launched', file=sys.stderr)
sys.exit(status)
''')
        self.stub('notify-send', "event('notify-send', args=args)\n")

    def stub(self, name, body):
        path = self.stubs / name
        path.write_text('#!' + str(PYTHON) + '\n' + STUB_PRELUDE + body)
        path.chmod(0o755)

    def events(self, name=None):
        entries = [json.loads(line) for line in self.events_file.read_text().splitlines()] \
            if self.events_file.exists() else []
        return [entry for entry in entries if name is None or entry['event'] == name]

    def run_helper(self, name, *args, input='\n', **env):
        return subprocess.run([str(self.bin / name), *args], env={**self.env, **env},
                              input=input, capture_output=True, text=True, timeout=10)

    def run_action(self, row, **env):
        return subprocess.run(['/bin/bash', '-c', self.rows[row]['action']],
                              env={**self.env, **env}, input='\n', capture_output=True,
                              text=True, timeout=10)

    def installs(self):
        return [entry['args'] for entry in self.events('pacman') if entry['args'][0] == '-S']

    def test_helpers_are_executable_manifested_and_exposed_on_image_path(self):
        manifest = json.loads((self.payload / 'arch-ipad-ui-manifest.json').read_text())
        for name in HELPERS:
            with self.subTest(helper=name):
                helper = self.bin / name
                self.assertTrue(helper.is_file(), name)
                self.assertTrue(helper.stat().st_mode & 0o111, name)
                key = helper.relative_to(self.payload).as_posix()
                self.assertEqual(manifest[key], hashlib.sha256(helper.read_bytes()).hexdigest())
                link = self.payload / 'usr/local/bin' / name
                self.assertTrue(link.is_symlink(), name)
                self.assertEqual(os.readlink(link), '/home/tablet/.local/share/omarchy-ipad/bin/' + name)

    def test_four_generic_ai_actions_reach_packages_and_expected_launches(self):
        expected = (
            ('install.ai.grok-bot', 'grok-bot', 'grok-bot'),
            ('install.ai.lm-studio', 'lmstudio-bin', None),
            ('install.ai.ollama', 'ollama', None),
            ('install.ai.perplexity', 'perplexity', 'perplexity'),
        )
        for row, package, launch in expected:
            with self.subTest(row=row):
                self.events_file.unlink(missing_ok=True)
                self.db.write_text('[]')
                result = self.run_action(row)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.installs(), [['-S', '--noconfirm', '--needed', '--', package]])
                self.assertEqual([entry['args'] for entry in self.events('gtk-launch')],
                                 [['--', launch]] if launch else [])
                self.assertEqual(len(self.events('foot')), 1)
                self.assertIn('--app-id=org.omarchy.terminal', self.events('foot')[0]['args'])
                self.assertNotIn('Press Enter to close', result.stdout + result.stderr)

    def test_five_deferred_ai_targets_fail_visibly_inside_terminal(self):
        deferred = ('chatgpt', 'claude', 'hermes', 'openclaw', 't3-code')
        self.assertEqual(len(self.rows), 9)
        for app in deferred:
            with self.subTest(app=app):
                self.events_file.unlink(missing_ok=True)
                result = self.run_action('install.ai.' + app)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 127, output)
                self.assertIn('omarchy-install-ai-' + app, output)
                self.assertIn('127', output)
                self.assertIn('Press Enter to close', output)
                self.assertEqual(len(self.events('foot')), 1)
                self.assertFalse(self.installs())

    def test_package_failure_is_retained_does_not_launch_and_can_be_retried(self):
        failed = self.run_action('install.ai.grok-bot', PACMAN_INSTALL_EXIT='37')
        output = failed.stdout + failed.stderr
        self.assertEqual(failed.returncode, 37, output)
        self.assertIn('target not found', output)
        self.assertIn('37', output)
        self.assertIn('Press Enter to close', output)
        self.assertFalse(self.events('gtk-launch'))
        self.assertEqual(json.loads(self.db.read_text()), [])
        result = self.run_action('install.ai.grok-bot')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([entry['args'] for entry in self.events('gtk-launch')], [['--', 'grok-bot']])

    def test_post_transaction_verification_failure_does_not_launch(self):
        result = self.run_action('install.ai.perplexity', PACMAN_VERIFY_MISSING='perplexity')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('perplexity', result.stdout + result.stderr)
        self.assertIn('Press Enter to close', result.stdout + result.stderr)
        self.assertEqual(len(self.installs()), 1)
        self.assertFalse(self.events('gtk-launch'))

    def test_sudo_cancellation_is_retained_without_installing_or_launching(self):
        result = self.run_helper('omarchy-install-and-launch', 'Fixture', 'fixture',
                                 'fixture', input='\n\n', SUDO_PROMPT='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Press Enter to close', result.stdout + result.stderr)
        self.assertFalse(self.installs())
        self.assertFalse(self.events('gtk-launch'))

    def test_already_installed_packages_do_not_repeat_sudo_transaction(self):
        self.db.write_text(json.dumps(['one', 'two']))
        result = self.run_helper('omarchy-pkg-add', 'one', 'two')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.installs())
        self.assertFalse(self.events('sudo'))

    def test_generic_wrapper_preserves_quoted_name_and_multi_package_arguments(self):
        sentinel = self.root / 'must-not-exist'
        title = 'Odd "name"; $(touch ' + str(sentinel) + ')'
        literal = 'three$(touch${IFS}' + str(sentinel) + ')'
        packages = ['one', 'two', literal]
        result = self.run_helper('omarchy-install-app', title, 'one\ttwo\n' + literal)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(self.installs(), [['-S', '--noconfirm', '--needed', '--', *packages]])
        self.assertEqual(json.loads(self.db.read_text()), sorted(packages))
        self.assertFalse(self.events('gtk-launch'))

    def test_desktop_handoff_failure_is_reported_as_launch_failure(self):
        result = self.run_action('install.ai.perplexity', LAUNCH_EXIT='23')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 23, output)
        self.assertIn('desktop entry could not be launched', output)
        self.assertIn('Press Enter to close', output)
        self.assertEqual(json.loads(self.db.read_text()), ['perplexity'])
        self.assertEqual(len(self.events('gtk-launch')), 1)

    def test_presentation_accepts_shell_command_and_preserves_failure_status(self):
        # 230 is Foot's own failure code too; a completed child using it must
        # still be presented as a command failure rather than a startup error.
        for status in (41, 230):
            with self.subTest(status=status):
                result = self.run_helper(PRESENT, "printf 'fixture shell command\\n'; exit " + str(status))
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, status, output)
                self.assertIn('fixture shell command', output)
                self.assertIn(str(status), output)
                self.assertIn('Press Enter to close', output)
                self.assertFalse(self.events('notify-send'))

    def test_presentation_preserves_multiple_shell_fragments(self):
        result = self.run_helper(PRESENT, 'printf', "'%s\\n'", "'fragment value'")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('\nfragment value\n', result.stdout)
        self.assertNotIn('Press Enter to close', result.stdout + result.stderr)

    def test_terminal_startup_failure_is_logged_and_notified(self):
        result = self.run_helper(PRESENT, 'exit 0', FOOT_FAIL='17')
        self.assertNotEqual(result.returncode, 0)
        log = self.root / 'state/omarchy/install/terminal-error.log'
        self.assertTrue(log.is_file(), result.stdout + result.stderr)
        self.assertIn('cannot connect to Wayland', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o077, 0)
        notification = self.events('notify-send')
        self.assertEqual(len(notification), 1)
        self.assertIn(str(log), ' '.join(notification[0]['args']))
        self.assertIn('critical', ' '.join(notification[0]['args']))

    def test_missing_terminal_is_logged_and_notified(self):
        (self.stubs / 'foot').unlink()
        result = self.run_helper(PRESENT, 'exit 0')
        self.assertNotEqual(result.returncode, 0)
        log = self.root / 'state/omarchy/install/terminal-error.log'
        self.assertTrue(log.is_file(), result.stdout + result.stderr)
        self.assertIn('foot', log.read_text().lower())
        self.assertEqual(len(self.events('notify-send')), 1)

    def test_terminal_exit_without_starting_runner_is_not_success(self):
        result = self.run_helper(PRESENT, 'exit 0', FOOT_SKIP_RUNNER='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'state/omarchy/install/terminal-error.log').is_file())
        self.assertEqual(len(self.events('notify-send')), 1)

    def test_missing_required_staged_helper_cannot_pass_action_test(self):
        # Work only on the temporary staged payload, never the source scripts.
        helper = self.bin / 'omarchy-pkg-add'
        hidden = helper.with_name(helper.name + '.hidden')
        helper.rename(hidden)
        try:
            result = self.run_action('install.ai.lm-studio')
        finally:
            hidden.rename(helper)
        self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
        self.assertIn('omarchy-pkg-add', result.stdout + result.stderr)
        self.assertIn('Press Enter to close', result.stdout + result.stderr)
        self.assertFalse(self.installs())

    def test_sudo_has_terminal_and_failure_waits_for_acknowledgment(self):
        master, slave = pty.openpty()
        process = subprocess.Popen([str(self.bin / 'omarchy-install-app'), 'Fixture', 'fixture'],
                                   env={**self.env, 'SUDO_REQUIRE_TTY': '1', 'SUDO_PROMPT': '1',
                                        'PACMAN_INSTALL_EXIT': '43'},
                                   stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()

        def read_until(text):
            deadline = time.monotonic() + 8
            while text.encode() not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail('Timed out waiting for ' + text + ': ' + output.decode(errors='replace'))
                ready, _, _ = select.select([master], [], [], remaining)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b''
                    if not data:
                        self.fail('Terminal closed before ' + text + ': ' + output.decode(errors='replace'))
                    output.extend(data)

        try:
            read_until('[sudo] password for tablet:')
            self.assertIsNone(process.poll())
            os.write(master, b'fixture-password\n')
            read_until('Press Enter to close')
            self.assertIsNone(process.poll(), output.decode(errors='replace'))
            self.assertTrue(self.events('sudo')[0]['tty'])
            self.assertIn(b'43', output)
            os.write(master, b'\n')
            self.assertEqual(process.wait(timeout=5), 43, output.decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
