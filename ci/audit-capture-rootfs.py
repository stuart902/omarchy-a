#!/usr/bin/env python3
"""Read-only coldplug/capture prerequisites in the reused desktop root."""
import argparse
import posixpath
import re
import tarfile


def audit(path):
    with tarfile.open(path, 'r:*') as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix('./').rstrip('/')
            if not name:
                continue
            if name.startswith('/') or '..' in name.split('/') or name in members:
                raise ValueError(f'Invalid or duplicate archive path: {name}')
            members[name] = member

        def resolve(name):
            seen = set()
            for _ in range(32):
                if name in seen or name not in members:
                    raise ValueError(f'Missing or cyclic archive target: {name}')
                seen.add(name)
                member = members[name]
                if member.isfile():
                    return member
                if not (member.issym() or member.islnk()):
                    raise ValueError(f'Not a regular file: {name}')
                target = member.linkname
                if target.startswith('/'):
                    name = target.lstrip('/')
                elif member.islnk():
                    name = target.removeprefix('./')
                else:
                    name = posixpath.join(posixpath.dirname(name), target)
                name = posixpath.normpath(name)
                if name == '..' or name.startswith('../'):
                    raise ValueError('Archive link escapes root')
            raise ValueError('Archive link depth exceeded')

        def read(name):
            member = resolve(name)
            if member.size > 1024 * 1024:
                raise ValueError(f'Oversized configuration file: {name}')
            return archive.extractfile(member).read().decode('utf-8')

        def executable(name):
            member = resolve(name)
            if not member.mode & 0o111:
                raise ValueError(f'Not executable: {name}')

        def effective(relative):
            for prefix in ('etc/', 'run/', 'usr/lib/'):
                name = prefix + relative
                if name in members:
                    return name
            raise ValueError(f'Missing configuration: {relative}')

        executable('usr/lib/systemd/systemd-udevd')
        executable('usr/bin/kmod')
        for unit in ('systemd-udevd.service', 'systemd-udev-trigger.service'):
            dropins = tuple(prefix + 'systemd/system/' + unit + '.d/' for prefix in ('etc/', 'run/'))
            if any(name.startswith(dropins) and name.endswith('.conf') for name in members):
                raise ValueError(f'Coldplug service drop-in requires review: {unit}')
            contents = read(effective('systemd/system/' + unit))
            if not re.search(r'^ExecStart=', contents, re.M):
                raise ValueError(f'Coldplug unit lacks ExecStart: {unit}')
        rules = read(effective('udev/rules.d/80-drivers.rules'))
        rules = '\n'.join(line.split('#', 1)[0] for line in rules.splitlines())
        if not re.search(r'kmod\s+load', rules):
            raise ValueError('Effective driver coldplug rule does not load modules')
        affected = re.compile(r'(?:^snd|aop|pdm|sio|dmic|max98357|simple.card|s8000.mca|phram|rtkit|apple.*mailbox)', re.I)
        for name in members:
            if name.startswith(('etc/modprobe.d/', 'run/modprobe.d/', 'usr/lib/modprobe.d/')) and name.endswith('.conf'):
                for line in read(name).splitlines():
                    words = line.split('#', 1)[0].strip().split()
                    if len(words) >= 2 and words[0] in ('blacklist', 'install') and affected.search(words[1]):
                        raise ValueError(f'Audio module override requires review: {name}')
        if any('aop-ipad7b' in name for name in members):
            raise ValueError('Private Apple AOP firmware present in public root')
        capture = []
        for name in ('usr/bin/arecord', 'usr/bin/pw-record'):
            if name in members:
                executable(name)
                capture.append(name)
        if not capture:
            raise ValueError('No existing ALSA or PipeWire recording command in root')
        print('Coldplug file prerequisites verified; recording commands:', ', '.join(capture))
        print('No checked module override or private AOP firmware found in public root')
        print('Archive checks do not establish runtime coldplug or desktop capture')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    audit(parser.parse_args().archive)
