#!/usr/bin/env python3
"""Assemble retained components with the exact menu staging from Omarchy PR #2.

This is a test-only assembly adapter. It does not change production sources,
compile components, access a device, or change any other desktop payload.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MENU = 'home/tablet/.local/share/omarchy-ipad/default/omarchy/omarchy-menu.jsonc'


def staged_menu(root=ROOT):
    text = (root / 'desktop/shell/default/omarchy/omarchy-menu.jsonc').read_text()
    old = '"when":"! omarchy-toggle-enabled suspend-off && ! omarchy-sleep-disabled"'
    new = '"when":"command -v ipadlinux-suspend-ready >/dev/null && ipadlinux-suspend-ready"'
    assert text.count(old) == 1, 'Unexpected menu staging input'
    text = text.replace(old, new)
    for name, verb in (('shutdown', 'poweroff'), ('reboot', 'reboot')):
        entry = re.search(r'"system\.' + name + r'":\s*(\{[^\n]+\})', text)
        assert json.loads(entry.group(1))['action'] == 'systemctl ' + verb
    return text.encode()


def assemble():
    from release import public_root, build
    original = public_root.desktop_replacements
    def replacements():
        result = original()
        assert MENU not in result
        result[MENU] = staged_menu()
        return result
    public_root.desktop_replacements = replacements
    build.main()


def verify(output):
    expected = staged_menu()
    found = {}
    with subprocess.Popen(['zstd', '-dc', str(output / 'rootfs.tar.zst')], stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                if member.name in (MENU, 'arch-ipad-ui-manifest.json'):
                    assert member.isfile()
                    found[member.name] = archive.extractfile(member).read()
        assert process.wait() == 0
    assert found[MENU] == expected, 'Packaged menu differs from PR #2 staging'
    digest = hashlib.sha256(expected).hexdigest()
    assert json.loads(found['arch-ipad-ui-manifest.json'])[MENU] == digest
    print('Verified packaged shutdown/reboot menu and desktop manifest:', digest)
    return digest


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--verify':
        verify(Path(sys.argv[2]))
    else:
        assemble()
