# J71b microphone package: bounded installation and removal

**Preparation is not device authorization or hardware qualification.** The
packaging machinery and offline fake-root tests do not access a tablet, start
an audio server, or prove safe capture. Do not run the device commands below
until the corresponding read-only preflight, installation, recording, or removal
operation has been separately authorized. Stop at the first mismatch or fault.

## Scope and provenance

The package adds exactly these four files, owned by root:root, mode 0644:

- `/usr/lib/omarchy-j71b-microphone/desktop_mic.py`
- `/usr/lib/omarchy-j71b-microphone/j71b-capture.so`
- `/usr/lib/systemd/user/omarchy-j71b-microphone.service`
- `/etc/omarchy/j71b-microphone.json`

There is no enable/start/restart/stop/reload action in the installer or remover.
There are no autostart links, package hooks, kernel/image installation, speaker
routing, power changes, default-device writes, or recording. Only absent paths
may be installed. Existing files, symlinks, masks and unit overrides are refused.
The remover preserves all persistent state, lock files and ownership markers;
it also leaves empty directories, because it cannot prove their ownership from
the four-file receipt. It never unloads/unbinds drivers or attempts recovery.

`MANIFEST.json` identifies the exact source commit and tree SHA, native artifact ZIP digest,
all payload hashes, source hash, ELF requirements and reviewed qualification
runs. The native plugin is the native ARM64 experiment Makefile output. The
separately compiled rootfs-helper binary was not retained in that artifact and
is **not** represented as this package's binary. The source of that helper is
included only as source. `SHA256SUMS` covers every other archive file. Compare
the archive SHA256 with the separately delivered value before extraction;
internal hashes provide integrity, not independent authentication.

Historical compatibility evidence identifies image
`preview-audio-nov4-41918815273`, kernel `7.3.0-rc1-j71b-audio-nov4-r1-orchard-apple-16k`, reused
root `preview-microphone-desktop-41717404430`, glibc 2.43, Python 3.14.9,
PipeWire 1.6.10, WirePlumber 0.5.18 and systemd 262.1. This is historical evidence,
not a fresh inspection of the installed tablet. The installer matches runtime
versions exactly. The operator must independently confirm the image identity
from installation records; `/etc/os-release` has a rolling build identifier
and cannot establish that artifact identity by itself. A current machine/boot,
os-release digest and selected raw-source serial are bound into preflight.

## Package construction, after qualification gates

This command is for the already-authorized development workspace, not a tablet.
Do not assemble a release while the synthetic-session or real systemd-manager
gates remain unresolved. The builder requires a clean committed tree, the
native artifact from that final commit, exact source correspondence, outer/inner
hash verification, and a reviewed JSON qualification record:

```json
{
  "release_decision": "approved-for-device-preflight",
  "synthetic_session": {"commit": "FULL_FINAL_COMMIT", "result": "passed", "url": "EXACT_GITHUB_ACTIONS_RUN_URL"},
  "systemd_manager": {"commit": "FULL_FINAL_COMMIT", "result": "passed", "url": "EXACT_GITHUB_ACTIONS_RUN_URL"},
  "repository_tests": {"commit": "FULL_FINAL_COMMIT", "result": "passed", "url": "EXACT_GITHUB_ACTIONS_RUN_URL"},
  "native_dsp": {"commit": "FULL_FINAL_COMMIT", "result": "passed", "url": "EXACT_GITHUB_ACTIONS_RUN_URL"}
}
```

These records must be filled from reviewed results, not used to bypass a failed
check. The builder validates their form and commit binding; it cannot establish
that an unauthenticated JSON assertion is a genuine CI result.

```sh
python3 tools/package_j71b_microphone.py \
  --native-zip /path/to/final-head-native-artifact.zip \
  --native-zip-sha256 VERIFIED_NATIVE_ZIP_SHA256 \
  --qualification /path/to/reviewed-qualification.json \
  --output /path/to/j71b-microphone-FINAL_COMMIT.tar.gz
```

Archive content: `payload/` with the four files; `package.py`; `MANIFEST.json`;
`SHA256SUMS`; this README; the self-contained desktop acceptance plan in `docs/j71b-microphone.md`; native `BUILD-INFO.txt`; `LICENSE`; and `source/`
containing DSP source, Makefile, C tests and the rootfs build helper. No full
rootfs image, recorder or separate rootfs-helper binary is included. All archive
members are regular files. Native ABI checks inspect ELF metadata without
loading the plugin: AArch64 ELF64 little-endian shared object, 16 KiB-compatible
load alignment, only libc/libm dependencies, no RPATH/RUNPATH, the exported
LADSPA entry point and declared GLIBC symbol requirements.

## Offline inspection

In the extracted package directory, these commands only inspect package files:

```sh
sha256sum --check --strict SHA256SUMS
python3 package.py verify
```

`verify` rejects unlisted/missing files, symlinks, hard links, changed hashes,
stale qualification records, source/receipt disagreements and wrong ABI.
A successful verification is not permission to install or start capture.

## Separately authorized read-only device preflight

Keep working USB serial and SSH. Keep speakers disabled. Close all recorders.
Use the existing desktop user, not root, for its audio session. The preflight
runs as root only so it can read complete kernel logs and inspect paths; its
bounded `runuser` commands read that existing user's manager and graph. It does
not start an audio service or create an alternate session.

Before using the command, inspect the full fresh current-boot kernel log via
serial and SSH. For the exact digest consumed by the tool:

```sh
sudo journalctl -k -b --no-pager -o cat > kernel-current-boot.txt
sha256sum kernel-current-boot.txt
```

Review the saved log yourself. Do not approve a PDM/AOP/SmartIO fault, timeout,
resource-retention or ownership message. Automatic keyword checks are a
fail-closed additional check, not proof of hardware health or DMA quiescence.
Resolve any pending AOP or touchscreen-health uncertainty before proceeding.
The operator must confirm that the tablet is currently responsive and healthy;
a log digest alone cannot establish this. Do not supply the health-confirmation
flag while a fault or hang remains unexplained. Use the just-reviewed digest
and independently verified installed image ID:

```sh
sudo python3 package.py preflight --operation install --user DESKTOP_USER \
  --image-id preview-audio-nov4-41918815273 \
  --confirm-hardware-healthy --reviewed-kernel-sha256 REVIEWED_LOG_SHA256 > install-preflight.json
```

The check requires exact J71b/T8010/AArch64 and runtime versions; readable
healthy current-boot logs; active existing PipeWire/WirePlumber with the running WirePlumber client
identity, main profile and version matching 0.5.18. Its main connection and
optional marked client-context connection must belong to the same server-verified
process/user/group, with distinct serials; conflicting or additional daemons
are refused; no audio/loader
overrides in the user manager; no matching unit, drop-in or generic service
override; no persistent marker; no supervisor/cgroup process; no failed/hung
unit; no conditioned nodes; and the precise two-channel raw ALSA source
idle/suspended with no consumer links and FL/FR channel positions/ports when
provided by the graph. Permission failures and timeouts refuse
the operation rather than weakening these checks. It does not inspect private
audio samples. Keep preflight reports private: they contain session identifiers.

## Separately authorized installation

The preflight report is valid for at most 120 seconds, on the same package,
boot, machine, user, raw-source identity and clean service/default state. The
installer reruns every read-only gate immediately before writing. If normal
new kernel messages changed the reviewed digest, stop and review a new log;
do not edit or reuse a stale report. Close recorders and do not manipulate the
session concurrently with installation/removal.

```sh
sudo python3 package.py install --user DESKTOP_USER --preflight install-preflight.json
```

All target checks complete before any mutation. Files are created exclusively,
without overwriting existing files; the user unit is created last. Files and
parent directories are synced. This is not a multi-file atomic transaction. An
I/O error or interruption can leave an incomplete package; preserve the error
and inspect it. Do not overwrite, rerun blindly, or treat file removal as
hardware recovery. No automatic rollback is performed after an uncertain write.

After authorized installation, the desktop user may explicitly refresh unit
metadata, as a separate step, without starting the microphone:

```sh
systemctl --user daemon-reload
systemctl --user cat omarchy-j71b-microphone.service
```

Do not enable the unit. Starting and recording require their own approved
hardware test; follow the included `docs/j71b-microphone.md` acceptance plan. That desktop
plan uses normal installed system tools and does not require the separate raw
experiment hardware-test harness.
Software qualification does not establish electrical idle, DMA release, safe
suspend/resume or acceptable real speech/noise/clipping.

## Separately authorized removal

Close all recording applications. If the service was started and is healthy,
request **one** normal bounded stop as the desktop user, separately from the
remover, then inspect its journal, graph and kernel evidence. Do not force-kill,
restart or repeatedly stop a hung service. A failed stop, retained marker,
remaining process/node, changed source or raw source that is still running
blocks removal. Preserve logs and stop testing; physical recovery may be needed.
A physical reset is not inferred from a software reboot or session recreation.

The normal stop command, only when that action is authorized:

```sh
systemctl --user stop omarchy-j71b-microphone.service
journalctl --user -b -u omarchy-j71b-microphone.service --no-pager
```

After clean stop (or if never started), obtain/review a new full kernel-log
digest and a removal preflight, then separately authorize file removal:

```sh
sudo python3 package.py preflight --operation remove --user DESKTOP_USER \
  --image-id preview-audio-nov4-41918815273 \
  --confirm-hardware-healthy --reviewed-kernel-sha256 REVIEWED_LOG_SHA256 > remove-preflight.json
sudo python3 package.py remove --user DESKTOP_USER --preflight remove-preflight.json
```

Removal first validates **all four** installed hashes, ownerships, modes and
regular-file identities. It reruns the current clean-stop/graph/marker checks,
then removes only those matching files, with the user unit first. Any changed,
missing, linked or stale file refuses the operation before deletion. A mid-write
or mid-removal filesystem failure can still require manual inspection; the
tool never follows it with automatic cleanup or driver recovery. Persistent
state and all directories remain. A separate authorized desktop-user
`systemctl --user daemon-reload` can refresh metadata afterward.

## Offline test command and limitations

```sh
orchard python -m unittest discover -s tests -p test_j71b_microphone_package.py
```

Tests use temporary fake roots, synthetic nonexecutable ELF metadata and mocked
read-only checkers. They never contact a real systemd manager, audio socket or
device. They cover four-file round-trip/modes, retained state, stale or changed
preflight, absent/failed qualification gates, hash/ABI/source disagreement,
existing paths, links, writable parents, exact removal and late marker refusal.
Temporary synthetic archives made by tests are removed automatically. These
are not released packages or evidence of device validation.
