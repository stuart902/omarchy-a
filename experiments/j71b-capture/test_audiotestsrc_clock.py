"""Socket-free starvation regression against unchanged pinned SPA source."""
from pathlib import Path
import subprocess
import sys
import tempfile


def main(source):
    expected = 'f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171'
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()
    assert revision == expected, 'Requires pinned PipeWire 1.6.10'
    subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD', '--',
                    'spa/plugins/audiotestsrc', 'spa/include'], check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix='j71b-audiotestsrc-clock-') as temp:
        binary = Path(temp) / 'test-audiotestsrc-clock'
        subprocess.run(['cc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-missing-field-initializers',
                        '-I', str(source / 'spa/include'),
                        '-I', str(source / 'spa/plugins/audiotestsrc'),
                        str(Path(__file__).with_suffix('.c')), '-lm', '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
