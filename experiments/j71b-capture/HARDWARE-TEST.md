# One-session J71b desktop microphone test

This is an opt-in acceptance test for the already installed microphone-only
J71b desktop, not an installer or production release. No kernel, boot files,
services, default devices or speaker routes are changed. The filter connects
as a client to the desktop's existing PipeWire server. It does not start a
private server. It only exists while the command runs.

## Risk and stopping rule

The experimental capture driver can retain resources indefinitely after a
PDM/AOP/SmartIO ownership or teardown fault. Closing a stream, suspend or
shutdown can hang, and a command timeout cannot make kernel teardown safe.
If the command reports a fault or hang, stop testing. Do not run it again,
unload/unbind drivers, or attempt software reboot/shutdown. A physical reset
may be necessary. Save the logs if possible and report the failure first.

This first test is one recording, not repeated open/close qualification. A
successful headless CI result does not prove desktop routing or hardware
lifecycle. The script checks what it can observe and refuses a mismatched
board, ambiguous microphone, active capture, ALSA playback node, unexpected
filter links, changed microphone identity, or changed default source.
A polling check is not a guarantee against every session-manager race.
All test clients use private, minimal configuration files instead of the
normal client rules. Environment overrides that could change daemon mode,
libraries or audio routing cause preflight to stop.

## Requirements

- iPad7,5 / J71b / T8010, with the existing microphone-only desktop installed.
  Device-tree compatible must include `apple,j71b` and `apple,t8010`.
- An ordinary desktop-user terminal or the existing SSH session to the iPad.
  Run the launcher as that user, never with sudo.
- Existing PipeWire 1.6.10 tools: `pipewire`, `pw-dump`, `pw-record`, Python 3.
  No compiler, NumPy, new package installation, or service restart is required.
- Close other recording applications. Keep the speaker-disabled test image.
  This package contains no playback command. Listen later on the Mac.

## First test

Extract the complete `j71b-microphone-test-aarch64.tar.gz` on the iPad and enter
its `j71b-microphone-test` folder. Do not mix files from different builds.

An SSH login may not inherit the desktop session's runtime environment. In
that SSH shell, set it only after verifying the current user's existing
PipeWire socket. This does not start or restart an audio server. If the check
fails, stop and report it; do not select another user's socket or run as root.

```sh
runtime_dir="/run/user/$(id -u)"
if test -d "$runtime_dir" && test -O "$runtime_dir" && \
   test -S "$runtime_dir/pipewire-0" && test -O "$runtime_dir/pipewire-0"; then
    export XDG_RUNTIME_DIR="$runtime_dir" PIPEWIRE_RUNTIME_DIR="$runtime_dir"
else
    printf 'STOP: no existing PipeWire socket owned by this SSH user.\n'
    exit 1
fi
```

```sh
cd j71b-microphone-test
sha256sum -c SHA256SUMS
cat BUILD-INFO.txt
python3 hardware_test.py --gain-db 12
```

The last command is inspection only. It checks the board, idle microphone,
source identity and kernel journal, then runs a native DSP self-test without
opening audio. If journal access is unavailable, export it explicitly:

```sh
printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)" > kernel-before.txt
sudo journalctl -k -b --no-pager -o cat >> kernel-before.txt
python3 hardware_test.py --gain-db 12 --kernel-log kernel-before.txt
```

The export must identify this boot and be less than two minutes old. Do not
proceed if any checksum, self-test or preflight check fails. When preflight
passes, run exactly one capture:

```sh
python3 hardware_test.py --run --gain-db 12 --output j71b-conditioned-12dB.wav
```

If using the journal export, refresh it first and append
`--kernel-log kernel-before.txt` to that command. The script creates a separate
source, pins input to the microphone's current object serial, disables
fallback/reconnection, verifies the two input links and idle state, counts
down three seconds, and records ten seconds through `pw-record`. Speak at your
normal distance and volume. It checks that the raw microphone returns to idle,
then stops only its own filter client and checks that its nodes disappeared.
There is no automatic retry or second recording.

Gain is explicit: +12 dB is a first test value, not a calibrated default.
The CLI also permits 0 and +24 dB for later approved comparisons. Do not jump
to +24 dB or repeat a run until this recording and fresh kernel log have been
checked. Fixed gain also raises noise. The -1 dBFS emergency bound clips
rather than transparently limiting loud input.

## Results to inspect before another capture

After the command returns, collect a fresh kernel log:

```sh
sudo journalctl -k -b --no-pager -o cat > kernel-after.txt
wpctl status
```

Keep the WAV, `kernel-after.txt`, and the session log folder printed by the
launcher. That folder contains before/after graph snapshots, configuration,
and filter/recorder logs. These stay local; the script uploads nothing.
The WAV is created with owner-only permissions, and its header and actual
PCM length are checked before success is reported. Recording statistics in
the session folder identify samples near the emergency-bound region; these
are not a substitute for hearing the recording or a calibrated gain policy.

Upstream PipeWire 1.6.10 can return status 1 when `pw-record --sample-count`
successfully reaches its frame limit. The launcher accepts that specific status
only with an observed correct stereo route, an empty recorder diagnostic log,
and exactly ten seconds of valid PCM. Other statuses, missing route evidence,
diagnostics, or incomplete files fail the test. Idle, node removal, default
identity and kernel checks remain separate requirements. The session stores
`recorder-outcome.json`, the first verified `recording.json` graph, and
`last-record.json` so status and route evidence are not lost. A valid WAV alone
does not prove safe teardown.

If an older package reports `Recording failed or two-channel route was not
observed` but leaves a WAV, preserve that file and the logs for inspection.
Do not repeat capture to resolve a launcher error.

Share the WAV and relevant log output for review. Do not upload the entire
folder publicly: snapshots can include unrelated local application details.

Check voice level and beginning/end pops on the Mac, not the iPad speakers.
The DC baseline should be greatly reduced. Arbitrary recording endings are
not guaranteed click-free, and this test does not prove that repeated capture,
suspend or shutdown is safe. Do not proceed to those tests automatically.

## Cleanup

Normal completion or Ctrl-C asks only this recorder and filter client to stop.
The script never unloads a driver, restarts PipeWire, edits configuration under
`~/.config`, or changes defaults. If either child does not stop within five
seconds, it reports the failure and does not force-kill or restart anything.
Keep the session logs. Once cleanup is confirmed, the package directory may
be removed manually; nothing was installed elsewhere.
