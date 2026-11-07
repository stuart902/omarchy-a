"""Run the repository bootstrap with real GPG inspection and pacman parsing.

Only package installation and pacman-key's trust mutations are substituted;
the actual bounded downloader and repository configuration helper run unchanged.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
PACMAN_CONF = shutil.which('pacman-conf')
FINGERPRINT = '40DFB630FF42BCFFB047046CF0134EE680CAC571'
BASE_CONFIG = '''[options]
Architecture = aarch64
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional
CheckSpace
DownloadUser = alpm

[core]
Server = https://mirror.archlinuxarm.org/$arch/$repo
[extra]
Server = https://mirror.archlinuxarm.org/$arch/$repo
[alarm]
Server = https://mirror.archlinuxarm.org/$arch/$repo
[aur]
Server = https://mirror.archlinuxarm.org/$arch/$repo
'''

# These are the external mutation boundaries, not replacements for the helper,
# GPG's public-key inspection, the parser, or the download/retry script.
BOUNDARY = '''#!{python}
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
event = {{'command': Path(sys.argv[0]).name, 'args': args}}
if '--config' in args:
    event['config'] = Path(args[args.index('--config') + 1]).read_text()
if '--gpgdir' in args:
    directory = Path(args[args.index('--gpgdir') + 1])
    event['gpgdir_exists'] = directory.is_dir()
    event['gpgdir_mode'] = directory.stat().st_mode & 0o777
with open(os.environ['PACKAGE_SOURCE_LOG'], 'a') as stream:
    stream.write(json.dumps(event) + '\\n')
phase = 'download' if '--downloadonly' in args else 'install'
if event['command'] == 'pacman' and os.environ.get('PACKAGE_SOURCE_FAIL') == phase:
    sys.exit(42 if phase == 'download' else 43)
'''


@unittest.skipUnless(BASH and PACMAN_CONF and shutil.which('gpg') and
                     shutil.which('gpgconf') and shutil.which('timeout'),
                     'needs bash, pacman-conf, GnuPG and timeout, as in the builders')
class PackageSources(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='omarchy-package-sources-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.target = self.work / 'target'
        (self.target / 'etc').mkdir(parents=True)
        self.config = self.target / 'etc/pacman.conf'
        self.config.write_text(BASE_CONFIG)
        # A target keyring must never be selected as the builder's trust store.
        self.target_gpg = self.target / 'etc/pacman.d/gnupg'
        self.target_gpg.mkdir(parents=True)
        (self.target_gpg / 'sentinel').write_text('existing target trust\n')
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        for name in ('pacman', 'pacman-key'):
            executable = self.bin / name
            executable.write_text(BOUNDARY.format(python=sys.executable))
            executable.chmod(0o755)
        self.scratch = self.work / 'scratch'
        self.scratch.mkdir()
        self.mirrorlist = self.work / 'mirrorlist'
        self.mirrorlist.write_text('Server = https://mirror.archlinuxarm.org/$arch/$repo\n')
        self.log = self.work / 'calls.jsonl'
        self.env = {
            **os.environ,
            'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
            'TMPDIR': str(self.scratch),
            'PACKAGE_SOURCE_LOG': str(self.log),
            'PACMAN_DOWNLOAD_ATTEMPTS': '1',
            'PACMAN_DOWNLOAD_SECONDS': '10',
            'PACMAN_DOWNLOAD_MIRRORLIST': str(self.mirrorlist),
        }

    def run_helper(self, project=PROJECT, fail=None):
        env = dict(self.env)
        if fail:
            env['PACKAGE_SOURCE_FAIL'] = fail
        return subprocess.run([BASH, str(project / 'system/configure-omarchy-repository'),
                               str(self.target)], env=env, capture_output=True,
                              text=True, timeout=30)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def parsed(self, *args, config=None):
        result = subprocess.run([PACMAN_CONF, '--config', str(config or self.config), *args],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def assert_isolated_and_cleaned(self, events):
        directories = {event['args'][event['args'].index('--gpgdir') + 1]
                       for event in events if '--gpgdir' in event['args']}
        self.assertEqual(len(directories), 1)
        gpgdir = Path(directories.pop())
        self.assertTrue(gpgdir.is_relative_to(self.scratch))
        self.assertFalse(gpgdir.is_relative_to(self.target))
        self.assertFalse(gpgdir.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        for event in events:
            self.assertTrue(event['gpgdir_exists'])
            self.assertEqual(event['gpgdir_mode'], 0o700)
        self.assertEqual([p.name for p in self.target_gpg.iterdir()], ['sentinel'])
        self.assertEqual((self.target_gpg / 'sentinel').read_text(), 'existing target trust\n')

    def test_adds_rolling_arm64_repository_after_alarm_without_weakening_signatures(self):
        official_signatures = {repo: self.parsed('--repo', repo, 'SigLevel')
                               for repo in ('core', 'extra', 'alarm', 'aur')}
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.parsed('--repo-list'), ['core', 'extra', 'alarm', 'aur', 'omarchy'])
        self.assertEqual(self.parsed('--repo', 'omarchy', 'Server'),
                         ['https://pkgs.omarchy.org/stable/aarch64'])
        self.assertEqual(self.parsed('--repo', 'omarchy', 'SigLevel'), [
            'PackageRequired', 'PackageTrustedOnly', 'DatabaseOptional', 'DatabaseTrustedOnly'])
        for repo, signatures in official_signatures.items():
            self.assertEqual(self.parsed('--repo', repo, 'SigLevel'), signatures)
        self.assertIn('DownloadUser = alpm\n', self.config.read_text())
        self.assertNotIn('DisableSandbox', self.config.read_text())

        events = self.events()
        key_events = [event for event in events if event['command'] == 'pacman-key']
        self.assertEqual([event['args'][2:] for event in key_events], [
            ['--init'], ['--populate', 'archlinuxarm'],
            ['--add', str(PROJECT / 'system/omarchy-pkgs.asc')], ['--lsign-key', FINGERPRINT]])
        transactions = [event for event in events if event['command'] == 'pacman']
        self.assertEqual(len(transactions), 2)
        download, install = transactions
        self.assertEqual(download['args'][:6], install['args'][:6])
        self.assertEqual(download['args'][2:4], ['--root', str(self.target)])
        self.assertEqual(download['args'][6:],
                         ['-Sy', '--noconfirm', '--needed', 'omarchy/omarchy-keyring', '--downloadonly'])
        self.assertEqual(install['args'][6:],
                         ['-S', '--noconfirm', '--needed', 'omarchy/omarchy-keyring'])
        temporary_config = Path(download['args'][1])
        self.assertNotEqual(temporary_config, self.config)
        self.assertTrue(temporary_config.is_relative_to(self.scratch))
        self.assertFalse(temporary_config.exists())
        self.assertEqual(download['config'], install['config'])
        self.assertNotIn('DownloadUser', download['config'])
        self.assertIn('DisableSandboxFilesystem', download['config'])
        self.assertIn('DisableSandboxSyscalls', download['config'])
        snapshot = self.work / 'transaction.conf'
        snapshot.write_text(download['config'])
        self.assertEqual(self.parsed('--repo', 'omarchy', 'Server', config=snapshot),
                         ['https://pkgs.omarchy.org/stable/aarch64'])
        self.assertEqual(self.parsed('--repo', 'omarchy', 'SigLevel', config=snapshot),
                         self.parsed('--repo', 'omarchy', 'SigLevel'))
        self.assert_isolated_and_cleaned(events)

    def test_failed_download_or_install_keeps_repository_disabled_and_cleans_temporary_trust(self):
        for phase, expected_status, expected_transactions in (('download', 42, 1), ('install', 43, 2)):
            with self.subTest(phase=phase):
                self.log.unlink(missing_ok=True)
                result = self.run_helper(fail=phase)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(self.config.read_text(), BASE_CONFIG)
                self.assertEqual(self.parsed('--repo-list'), ['core', 'extra', 'alarm', 'aur'])
                events = self.events()
                self.assertEqual(sum(event['command'] == 'pacman' for event in events),
                                 expected_transactions)
                self.assert_isolated_and_cleaned(events)

    def test_wrong_architecture_is_rejected_before_download_or_trust_mutation(self):
        wrong_arch = BASE_CONFIG.replace('Architecture = aarch64', 'Architecture = x86_64')
        self.config.write_text(wrong_arch)
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires an aarch64', result.stderr)
        self.assertEqual(self.config.read_text(), wrong_arch)
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_unexpected_valid_bootstrap_key_is_rejected_before_download_or_trust_mutation(self):
        project = self.work / 'project'
        system = project / 'system'
        system.mkdir(parents=True)
        for name in ('configure-omarchy-repository', 'omarchy-repository.conf', 'pacman-download'):
            shutil.copy2(PROJECT / 'system' / name, system / name)
        key_home = self.work / 'untrusted-key'
        key_home.mkdir(mode=0o700)
        gpg = ['gpg', '--batch', '--no-options', '--homedir', str(key_home)]
        try:
            subprocess.run([*gpg, '--pinentry-mode', 'loopback', '--passphrase', '',
                            '--quick-generate-key', 'Untrusted package publisher', 'ed25519', 'sign', '0'],
                           check=True, capture_output=True, timeout=20)
            key = subprocess.run([*gpg, '--armor', '--export'], check=True,
                                 capture_output=True, timeout=10).stdout
            (system / 'omarchy-pkgs.asc').write_bytes(key)
            result = self.run_helper(project=project)
        finally:
            subprocess.run(['gpgconf', '--homedir', str(key_home), '--kill', 'gpg-agent'],
                           capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unexpected fingerprint', result.stderr)
        self.assertEqual(self.config.read_text(), BASE_CONFIG)
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_existing_repository_is_rejected_without_changing_config_or_trust(self):
        configured = BASE_CONFIG + (PROJECT / 'system/omarchy-repository.conf').read_text()
        self.config.write_text(configured)
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already configured', result.stderr)
        self.assertEqual(self.config.read_text(), configured)
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
