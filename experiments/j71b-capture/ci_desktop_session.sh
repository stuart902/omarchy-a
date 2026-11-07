#!/usr/bin/env bash
# CI-only provisioning for a private, synthetic PipeWire/WirePlumber session.
# Installs into /usr ONLY inside this job's disposable Debian container so the
# production supervisor exercises its real, absolute /usr/bin tool paths.
# This does not qualify hardware or a systemd user-manager lifecycle.
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: bash $0 BUILD_DIRECTORY LOG_DIRECTORY" >&2
  exit 2
fi
if [[ ${GITHUB_ACTIONS:-} != true || ${RUNNER_ENVIRONMENT:-} != github-hosted ||
      ! -f /.dockerenv || $EUID != 0 ]]; then
  echo 'Run only in the disposable GitHub-hosted CI job container.' >&2
  exit 2
fi
if ! grep -qx 'ID=debian' /etc/os-release ||
   ! grep -qx 'VERSION_CODENAME=trixie' /etc/os-release; then
  echo 'This provisioning script requires the Debian trixie job container.' >&2
  exit 2
fi
if [[ -e /dev/snd || -e /dev/bus/usb ]]; then
  echo 'Refusing a container with audio or USB devices exposed.' >&2
  exit 2
fi

runner_temp=$(realpath "${RUNNER_TEMP:?RUNNER_TEMP is required}")
build_dir=$(realpath -m "$1")
log_dir=$(realpath -m "$2")
for directory in "$build_dir" "$log_dir"; do
  case "$directory" in
    "$runner_temp"/*) ;;
    *) echo 'Build and log directories must be under RUNNER_TEMP.' >&2; exit 2 ;;
  esac
done
[[ ! -e "$build_dir" ]] || {
  echo 'Use a fresh build directory; this job never reuses upstream builds.' >&2
  exit 2
}
experiment_dir=$(cd -- "$(dirname -- "$0")" && pwd)
readonly pipewire_version=1.6.10
readonly pipewire_commit=f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171
readonly wireplumber_version=0.5.18
readonly wireplumber_commit=81606cbb32d29df20147d1b68b662c1082654d81

install -d -m 0755 "$build_dir" "$log_dir"
exec > >(tee -a "$log_dir/provision.log") 2>&1
ci_uid=''
finish() {
  result=$?
  trap - EXIT
  # Some fault-test children have their own process groups. The timeout still
  # bounds the harness; clean up only this newly created account's descendants
  # if the harness was interrupted before its normal teardown could finish.
  if [[ -n "$ci_uid" ]]; then
    pkill --signal KILL --uid "$ci_uid" || true
  fi
  for project in pipewire wireplumber; do
    diagnostic="$build_dir/$project-build/meson-logs/meson-log.txt"
    if [[ -f "$diagnostic" ]]; then
      cp "$diagnostic" "$log_dir/$project-meson.log" || true
    fi
  done
  printf 'CI provisioning/session exit status: %s\n' "$result" \
    > "$log_dir/result.txt"
  exit "$result"
}
trap finish EXIT

fetch_pinned() {
  local project=$1 version=$2 expected=$3
  local source_dir="$build_dir/$project"
  git clone --depth 1 --branch "$version" \
    "https://github.com/PipeWire/$project.git" "$source_dir"
  [[ $(git -C "$source_dir" rev-parse HEAD) == "$expected" ]] || {
    echo "$project $version does not match pinned commit $expected" >&2
    return 1
  }
  git -C "$source_dir" diff --quiet HEAD --
  printf '%s %s %s\n' "$project" "$version" "$expected" \
    >> "$log_dir/source-pins.txt"
}
fetch_pinned pipewire "$pipewire_version" "$pipewire_commit"
fetch_pinned wireplumber "$wireplumber_version" "$wireplumber_commit"
python3 "$experiment_dir/test_audiotestsrc_clock.py" "$build_dir/pipewire"

multiarch=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
# No ALSA/Bluetooth/camera/device monitors, daemon service units, realtime/PAM
# changes, or downloaded Meson subprojects. pw-cat needs libsndfile explicitly.
meson setup "$build_dir/pipewire-build" "$build_dir/pipewire" \
  --prefix=/usr --libdir="lib/$multiarch" --sysconfdir=/etc \
  --buildtype=release --auto-features=disabled --wrap-mode=nodownload \
  -Ddbus=enabled -Dflatpak=disabled -Dpipewire-alsa=disabled \
  -Dpipewire-jack=disabled -Dpipewire-v4l2=disabled \
  -Dalsa=disabled -Dbluez5=disabled -Djack=disabled -Dv4l2=disabled \
  -Dlibcamera=disabled -Dudev=disabled -Dlibsystemd=disabled -Dlogind=disabled \
  -Dsystemd-system-service=disabled -Dsystemd-user-service=disabled \
  -Drlimits-install=false -Dpam-defaults-install=false -Dlegacy-rtkit=false \
  -Dexamples=disabled -Dtests=disabled '-Dsession-managers=[]' \
  -Dsndfile=enabled -Dpw-cat=enabled
meson compile -C "$build_dir/pipewire-build" -j 4
meson install -C "$build_dir/pipewire-build" --no-rebuild
ldconfig

# Link against the just-installed pinned PipeWire, not a distribution engine.
[[ $(pkg-config --modversion libpipewire-0.3) == "$pipewire_version" ]]
meson setup "$build_dir/wireplumber-build" "$build_dir/wireplumber" \
  --prefix=/usr --libdir="lib/$multiarch" --sysconfdir=/etc \
  --buildtype=release --auto-features=disabled --wrap-mode=nodownload \
  -Dsystem-lua=true -Dsystem-lua-version=5.4 -Dintrospection=disabled \
  -Dsystemd=disabled -Delogind=disabled \
  -Dsystemd-system-service=false -Dsystemd-user-service=false \
  -Ddoc=disabled -Dtests=false -Ddbus-tests=false
meson compile -C "$build_dir/wireplumber-build" -j 4
meson install -C "$build_dir/wireplumber-build" --no-rebuild
ldconfig

# Check both compiled and dynamically linked versions at the paths serve()
# actually uses. All required tools must be real installed executables.
for binary in pipewire pw-dump pw-cli pw-link pw-cat; do
  [[ -x /usr/bin/$binary ]]
  version=$(/usr/bin/"$binary" --version)
  printf '%s\n' "$version" | tee -a "$log_dir/runtime-versions.txt"
  grep -Fx "Compiled with libpipewire $pipewire_version" <<< "$version"
  grep -Fx "Linked with libpipewire $pipewire_version" <<< "$version"
done
[[ -x /usr/bin/wireplumber && -x /usr/bin/wpctl && -x /usr/bin/dbus-run-session ]]
version=$(/usr/bin/wireplumber --version)
printf '%s\n' "$version" | tee -a "$log_dir/runtime-versions.txt"
grep -Fx "Compiled with libwireplumber $wireplumber_version" <<< "$version"
grep -Fx "Linked with libwireplumber $wireplumber_version" <<< "$version"
test -r /usr/share/wireplumber/wireplumber.conf
make -C "$experiment_dir" all

# A dedicated unprivileged account and an empty environment prevent accidental
# access to a host session. The harness starts its own dbus-run-session and
# private runtime socket, with synthetic generators and no hardware monitors.
useradd --user-group --no-create-home --shell /usr/sbin/nologin j71b-ci
ci_uid=$(id -u j71b-ci)
install -d -m 0700 -o j71b-ci -g j71b-ci \
  "$build_dir/home" "$build_dir/runtime" "$log_dir/session"
printf 'Architecture: %s\nTest: synthetic session, no hardware or systemd manager\n' \
  "$(uname -m)" >> "$log_dir/runtime-versions.txt"
timeout --signal=TERM --kill-after=10s 240s \
  runuser -u j71b-ci -- env -i \
    PATH=/usr/bin:/bin HOME="$build_dir/home" \
    XDG_RUNTIME_DIR="$build_dir/runtime" TMPDIR="$build_dir/runtime" \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 GITHUB_ACTIONS=true \
    /usr/bin/python3 "$experiment_dir/test_desktop_session.py" \
      --run-isolated-session --output-directory "$log_dir/session" \
      --plugin "$experiment_dir/build/j71b-capture.so" \
      --wireplumber-config /usr/share/wireplumber/wireplumber.conf \
  2>&1 | tee "$log_dir/session.log"
