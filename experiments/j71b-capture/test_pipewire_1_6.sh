#!/usr/bin/env bash
# Build and exercise the pinned upstream engine without installing it.
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: bash $0 PIPEWIRE_SOURCE BUILD_DIRECTORY" >&2
  exit 2
fi
source_dir=$(realpath "$1")
build_dir=$(realpath -m "$2")
experiment_dir=$(cd -- "$(dirname -- "$0")" && pwd)
expected=f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171
[[ $(git -C "$source_dir" rev-parse HEAD) == "$expected" ]] || {
  echo "Expected the pinned upstream PipeWire 1.6.10 commit $expected" >&2
  exit 1
}
git -C "$source_dir" diff --quiet HEAD --
if [[ -f "$build_dir/meson-info/meson-info.json" ]]; then
  python3 - "$build_dir/meson-info/meson-info.json" "$source_dir" <<'PY'
import json
from pathlib import Path
import sys
info = json.loads(Path(sys.argv[1]).read_text())
if Path(info['directories']['source']).resolve() != Path(sys.argv[2]).resolve():
    raise SystemExit('Existing build directory belongs to a different checkout')
PY
fi

meson setup "$build_dir" "$source_dir" --auto-features=disabled \
  --wrap-mode=nodownload -Ddbus=disabled -Dflatpak=disabled \
  -Dpipewire-jack=disabled -Dpipewire-v4l2=disabled \
  -Dexamples=disabled -Dtests=disabled '-Dsession-managers=[]'
meson compile -C "$build_dir" -j 4

export LD_LIBRARY_PATH="$build_dir/src/pipewire${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export SPA_PLUGIN_DIR="$build_dir/spa/plugins"
export PIPEWIRE_NO_CONFIG=true
cd "$experiment_dir"
python3 - <<'PY'
from spa_graph import Graph, stock_graph
with Graph(stock_graph()) as graph:
    assert graph.version == '1.6.10', f'Wrong runtime loaded: {graph.version}'
    print('Verified pinned PipeWire runtime:', graph.version)
PY
make test
python3 spa_graph.py
python3 test_config_parser.py "$build_dir/src/tools/pw-config"
