#!/usr/bin/env python3
"""Synthetic platform audit checks; fdtget is mocked, not a hardware test."""
import contextlib
import io
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


MODULE = runpy.run_path(str(Path(__file__).with_name('audit-capture-platform.py')))
AUDIT = MODULE['audit']
RELEASE = '6.19.0-fixture-16k'
ROOT = 'lib/modules/' + RELEASE + '/'
LAYER_ROOT = 'usr/' + ROOT
DTB = b'synthetic DTB; fdtget is mocked\n'
STATUSES = {
    '/dmic': 'okay', '/microphones': 'okay',
    '/soc/pdm@20a0b4000': 'okay',
    '/soc/aop@210800000/aop-audio': 'okay',
    '/sound': 'disabled', '/soc/mca@20a0a8000': 'disabled',
}
ALIASES = {
    'apple-aop.ko': ('of:N*T*Capple,t8010-aop', 'apple_aop'),
    'aop-audio.ko': ('of:N*T*Capple,t8010-aop-audio', 'aop_audio'),
    'apple-sio.ko': ('of:N*T*Capple,t8010-sio', 'apple_sio'),
    'apple-sio-dma.ko': ('platform:apple-sio-dma', 'apple_sio_dma'),
    't8010-pdm.ko': ('of:N*T*Capple,j71b-pdm', 't8010_pdm'),
    'snd-soc-dmic.ko': ('of:N*T*Cdmic-codec', 'snd_soc_dmic'),
    'snd-soc-max98357a.ko': ('of:N*T*Cmaxim,max98357a', 'snd_soc_max98357a'),
    'snd-soc-simple-card.ko': ('of:N*T*Csimple-audio-card', 'snd_soc_simple_card'),
}
MODULE_PATHS = {name: 'kernel/fixture/' + name for name in MODULE['MODULES']}
CORE = 'kernel/fixture/snd-soc-core.ko'
PCM = 'kernel/fixture/snd-pcm.ko'


def base_files():
    files = {ROOT + path: ('fixture ' + name).encode()
             for name, path in MODULE_PATHS.items()}
    files.update({
        'kernel.release': (RELEASE + '\n').encode(),
        'dtbs/t8010-j71b.dtb': DTB,
        ROOT + CORE: b'fixture core', ROOT + PCM: b'fixture pcm',
        ROOT + 'modules.dep': ''.join(
            path + ': ' + CORE + '\n' for path in MODULE_PATHS.values()
        ).encode() + (CORE + ': ' + PCM + '\n' + PCM + ':\n').encode(),
        ROOT + 'modules.alias': ''.join(
            ''.join(f'alias {variant} {target}\n' for variant in ((alias, alias + 'C*') if alias.startswith('of:') else (alias,)))
            for alias, target in ALIASES.values()).encode(),
        ROOT + 'modules.dep.bin': b'fixture binary metadata',
        ROOT + 'modules.alias.bin': b'fixture binary metadata',
    })
    return files


def update(files, changes):
    for name, data in changes.items():
        if data is None:
            files.pop(name, None)
        else:
            files[name] = data


class PlatformAuditFixtures(unittest.TestCase):
    def check(self, *, changes=None, layer_changes=None, statuses=None,
              accepted=False, layer=False, fdt_error=False):
        files = base_files()
        update(files, changes or {})
        status = STATUSES | (statuses or {})
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / 'kernel'
            for name, data in files.items():
                path = kernel / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            installed = {LAYER_ROOT + 'dtb/t8010-j71b.dtb': DTB}
            installed.update({'usr/' + name: data for name, data in files.items()
                              if name.startswith(ROOT)})
            update(installed, layer_changes or {})
            output = Path(directory) / 'platform.tar.gz'
            if layer:
                with tarfile.open(output, 'w:gz') as archive:
                    for name, data in installed.items():
                        member = tarfile.TarInfo(name)
                        member.size, member.mode = len(data), 0o644
                        archive.addfile(member, io.BytesIO(data))

            def fdtget(command, text):
                self.assertEqual(command[:2], ['fdtget', str(kernel / 'dtbs/t8010-j71b.dtb')])
                self.assertTrue(text)
                self.assertEqual(command[3:], ['status'])
                if fdt_error:
                    raise subprocess.CalledProcessError(1, command)
                return status[command[2]] + '\n'

            with patch.object(MODULE['subprocess'], 'check_output', side_effect=fdtget) as mocked, \
                    contextlib.redirect_stdout(io.StringIO()):
                if accepted:
                    AUDIT(kernel, output if layer else None)
                    self.assertEqual({call.args[0][2] for call in mocked.call_args_list}, set(STATUSES))
                else:
                    with self.assertRaises((ValueError, FileNotFoundError, subprocess.CalledProcessError)):
                        AUDIT(kernel, output if layer else None)

    def test_valid_input(self):
        self.check(accepted=True)

    def test_valid_installed_layer(self):
        self.check(layer=True, accepted=True)

    def test_missing_required_module(self):
        self.check(changes={ROOT + MODULE_PATHS['apple-aop.ko']: None})

    def test_duplicate_required_module(self):
        self.check(changes={ROOT + 'another/apple-aop.ko': b'duplicate'})

    def test_missing_dependency_entry(self):
        lines = base_files()[ROOT + 'modules.dep'].decode().splitlines()
        self.check(changes={ROOT + 'modules.dep': '\n'.join(lines[1:]).encode()})

    def test_missing_direct_dependency_file(self):
        self.check(changes={ROOT + CORE: None})

    def test_missing_transitive_dependency_file(self):
        self.check(changes={ROOT + PCM: None})

    def test_missing_transitive_dependency_entry(self):
        lines = base_files()[ROOT + 'modules.dep'].decode().splitlines()
        self.check(changes={ROOT + 'modules.dep': '\n'.join(lines[:-1]).encode()})

    def test_missing_alias_metadata(self):
        self.check(changes={ROOT + 'modules.alias': None})

    def test_missing_binary_dependency_index(self):
        self.check(changes={ROOT + 'modules.dep.bin': None})

    def test_empty_binary_dependency_index(self):
        self.check(changes={ROOT + 'modules.dep.bin': b''})

    def test_missing_binary_alias_index(self):
        self.check(changes={ROOT + 'modules.alias.bin': None})

    def test_empty_binary_alias_index(self):
        self.check(changes={ROOT + 'modules.alias.bin': b''})

    def test_duplicate_dependency_entry(self):
        self.check(changes={ROOT + 'modules.dep': base_files()[ROOT + 'modules.dep'] +
                            (PCM + ':\n').encode()})

    def test_absolute_dependency(self):
        self.check(changes={ROOT + 'modules.dep': base_files()[ROOT + 'modules.dep'].replace(
            (CORE + ': ' + PCM).encode(), (CORE + ': /tmp/not-a-module.ko').encode())})

    def test_escaping_dependency(self):
        self.check(changes={ROOT + 'modules.dep': base_files()[ROOT + 'modules.dep'].replace(
            (CORE + ': ' + PCM).encode(), (CORE + ': ../not-a-module.ko').encode())})

    def test_aliases_are_not_comments(self):
        self.check(changes={ROOT + 'modules.alias': b'# ' + b'\n# '.join(
            base_files()[ROOT + 'modules.alias'].splitlines())})

    def test_aliases_target_correct_modules(self):
        self.check(changes={ROOT + 'modules.alias': base_files()[ROOT + 'modules.alias'].replace(
            b' t8010_pdm\n', b' wrong_module\n')})

    def test_missing_aop_parent_alias(self):
        self.check(changes={ROOT + 'modules.alias': base_files()[ROOT + 'modules.alias'].replace(
            b'alias of:N*T*Capple,t8010-aop apple_aop\n', b'').replace(
            b'alias of:N*T*Capple,t8010-aopC* apple_aop\n', b'')})

    def test_missing_sio_parent_alias(self):
        self.check(changes={ROOT + 'modules.alias': base_files()[ROOT + 'modules.alias'].replace(
            b'alias of:N*T*Capple,t8010-sio apple_sio\n', b'').replace(
            b'alias of:N*T*Capple,t8010-sioC* apple_sio\n', b'')})

    def test_missing_sio_dma_platform_alias(self):
        self.check(changes={ROOT + 'modules.alias': base_files()[ROOT + 'modules.alias'].replace(
            b'alias platform:apple-sio-dma apple_sio_dma\n', b'')})

    def test_mismatched_release(self):
        self.check(changes={'kernel.release': b'wrong-release\n'})

    def test_multiple_release_directories(self):
        self.check(changes={'lib/modules/other-release/module.ko': b'other release'})

    def test_wrong_dt_statuses(self):
        for node, expected in STATUSES.items():
            with self.subTest(node=node):
                self.check(statuses={node: 'disabled' if expected == 'okay' else 'okay'})

    def test_fdtget_failure(self):
        self.check(fdt_error=True)

    def test_wrong_installed_dtb_bytes(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'dtb/t8010-j71b.dtb': b'wrong DTB'})

    def test_missing_installed_dtb(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'dtb/t8010-j71b.dtb': None})

    def test_dtb_at_wrong_destination(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'dtb/t8010-j71b.dtb': None,
                                             'wrong/t8010-j71b.dtb': DTB})

    def test_missing_installed_module(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + MODULE_PATHS['apple-aop.ko']: None})

    def test_different_installed_module(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + MODULE_PATHS['apple-aop.ko']: b'changed module'})

    def test_different_installed_dependency_metadata(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'modules.dep': b'wrong metadata'})

    def test_different_installed_alias_metadata(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'modules.alias': b'wrong aliases'})

    def test_different_installed_binary_dependency_index(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'modules.dep.bin': b'wrong binary dependencies'})

    def test_different_installed_binary_alias_index(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'modules.alias.bin': b'wrong binary aliases'})

    def test_missing_installed_binary_alias_index(self):
        self.check(layer=True, layer_changes={LAYER_ROOT + 'modules.alias.bin': None})


if __name__ == '__main__':
    unittest.main()
