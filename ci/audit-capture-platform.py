#!/usr/bin/env python3
"""Check the full microphone platform input and its assembled DTB copy."""
import argparse
from pathlib import Path
import subprocess
import tarfile

MODULES = ('apple-aop.ko', 'aop-audio.ko', 'apple-sio.ko', 'apple-sio-dma.ko',
           't8010-pdm.ko', 'snd-soc-dmic.ko', 'snd-soc-max98357a.ko',
           'snd-soc-simple-card.ko', 'snd-soc-simple-card-utils.ko')


def audit(kernel, layer=None):
    kernel = Path(kernel)
    releases = list((kernel / 'lib/modules').iterdir())
    if len(releases) != 1 or not releases[0].is_dir():
        raise ValueError('Expected one matching module release')
    modules = releases[0]
    dependency_text = (modules / 'modules.dep').read_text()
    aliases = (modules / 'modules.alias').read_text()
    for name in ('modules.dep.bin', 'modules.alias.bin'):
        if not (modules / name).is_file() or (modules / name).stat().st_size == 0:
            raise ValueError(f'Missing binary module index: {name}')
    depends = {}
    for line in dependency_text.splitlines():
        name, separator, rest = line.partition(':')
        if not separator or name in depends:
            raise ValueError('Malformed or duplicate module dependency entry')
        depends[name] = rest.split()
    needed = set()
    pending = []
    for name in MODULES:
        matches = list(modules.rglob(name))
        if len(matches) != 1 or not matches[0].is_file():
            raise ValueError(f'Missing/ambiguous module: {name}')
        pending.append(matches[0].relative_to(modules).as_posix())
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        if Path(name).is_absolute() or '..' in Path(name).parts or not (modules / name).is_file():
            raise ValueError(f'Missing/invalid dependency: {name}')
        if name not in depends:
            raise ValueError(f'Missing dependency metadata: {name}')
        needed.add(name)
        pending.extend(depends[name])
    alias_lines = {tuple(line.split()) for line in aliases.splitlines() if not line.startswith('#')}
    for compatible, target in (
        ('apple,j71b-pdm', 't8010_pdm'),
        ('apple,t8010-aop-audio', 'aop_audio'),
        ('apple,t8010-aop', 'apple_aop'),
        ('apple,t8010-sio', 'apple_sio'),
        ('maxim,max98357a', 'snd_soc_max98357a'),
        ('dmic-codec', 'snd_soc_dmic'),
        ('simple-audio-card', 'snd_soc_simple_card'),
    ):
        if ('alias', 'of:N*T*C' + compatible, target) not in alias_lines:
            raise ValueError(f'Missing exact module alias: {compatible}')
    if ('alias', 'platform:apple-sio-dma', 'apple_sio_dma') not in alias_lines:
        raise ValueError('Missing SmartIO DMA child module alias')
    if (kernel / 'kernel.release').read_text().strip() != modules.name:
        raise ValueError('Kernel/module release mismatch')
    dtb = kernel / 'dtbs/t8010-j71b.dtb'
    for node, expected in (
        ('/dmic', 'okay'), ('/microphones', 'okay'),
        ('/soc/pdm@20a0b4000', 'okay'),
        ('/soc/aop@210800000/aop-audio', 'okay'),
        ('/sound', 'disabled'), ('/soc/mca@20a0a8000', 'disabled'),
    ):
        result = subprocess.check_output(['fdtget', str(dtb), node, 'status'], text=True).strip()
        if result != expected:
            raise ValueError(f'Unexpected DT status: {node}')
    if layer:
        with tarfile.open(layer, 'r:*') as archive:
            members = {}
            for member in archive:
                if member.name in members:
                    raise ValueError('Duplicate installed archive member')
                members[member.name] = member
            prefix = f'usr/lib/modules/{modules.name}/'
            expected = {prefix + 'dtb/t8010-j71b.dtb': dtb}
            expected.update({prefix + name: modules / name for name in needed})
            expected.update({prefix + name: modules / name for name in ('modules.dep', 'modules.alias', 'modules.dep.bin', 'modules.alias.bin')})
            for name, source in expected.items():
                member = members.get(name)
                if member is None or not member.isfile() or archive.extractfile(member).read() != source.read_bytes():
                    raise ValueError(f'Installed platform file differs or is missing: {name}')
    print('Full microphone modules, metadata and playback-disabled DTB verified')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel')
    parser.add_argument('--layer')
    args = parser.parse_args()
    audit(args.kernel, args.layer)
