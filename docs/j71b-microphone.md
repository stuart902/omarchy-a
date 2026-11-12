# J71b conditioned microphone

This adds an **opt-in, experimental desktop source**, not a completed hardware
qualification. The AArch64 rootfs build installs the plugin, a small Python
supervisor and `omarchy-j71b-microphone.service`. It is not enabled at login.
ARMv7 images do not build or install this AArch64-only payload. Cached ARMv7
bases containing any of its payload files are rejected rather than published.
No kernel, speaker routing, boot entries or default microphone settings change.

The intended application-visible source is **J71b Microphone (conditioned)**
(`j71b.microphone`). The raw ALSA microphone remains available. Applications may
record mono or stereo at their preferred rate through PipeWire's conversion.
Neither the service nor its supervisor records audio or plays sound.

## Application selection and level

The source exists only while the opt-in service is running. Select **J71b
Microphone (conditioned)** in the application's input chooser. Native PipeWire
clients use `j71b.microphone`; PulseAudio clients use the same source name through
the existing `pipewire-pulse` server. A successful `pw-record` capture establishes
the native client path, not a browser or other PulseAudio application's path.

The desktop audio panel already lists this exact `Audio/Source` media class.
Choosing its input row changes the user's default source, so that is an explicit
user choice, not an installation step. Apps with their own input chooser can
select it without changing the global default. The service does not start at
login or become the default automatically.

Do not infer a gain regression or choose a new gain from one file's peak. Compare
the same speech and microphone placement, with raw-source, conditioned-source
and application volume/mute settings recorded. Compare channel peaks, DC-free
RMS and clipping as well as listening; different phrases, distance and pauses
can change these measurements. The desktop input slider is separate from the
fixed DSP gain. Raising gain also raises noise, and the DSP emergency bound does
not protect against amplification or clipping later in an application. Keep the
candidate at +12 dB until representative evidence supports a different policy.

## Conditioning

The same reviewed LADSPA component provides independent stereo 80 Hz DC
rejection, a 20 ms startup fade, smoothed gain and a -1 dBFS emergency output
bound. The desktop candidate uses **+12 dB**, based on a positive listening test,
not broad calibration. Fixed gain also raises noise; the emergency clamp can
distort. Arbitrary recording endings and unknown midstream DC jumps are not
guaranteed click-free. Overload counters reset when the DSP activates; they are
not a lifetime monitoring system.

## Startup and ownership

The service requires an already-running desktop PipeWire and WirePlumber. It
never starts a private or system audio daemon. It is restricted to AArch64 with
both `apple,j71b` and `apple,t8010` device compatibility strings and PipeWire
1.6.10, the version exercised by the capture tests.

Before creating its client filter it checks the exact two-channel ALSA source,
card name and capture direction, then pins that source's object serial. It does
not fall back or reconnect. Its input links must preserve FL/FR. Ordinary
application consumers and intentional user default selections are allowed.

The input is passive and should run only while applications consume audio.
Source/link errors, source replacement, lost input links, server disconnect,
unexpected filter exit and failure to return to idle stop the supervisor and
leave an ownership marker. The supervisor observes the PipeWire graph, not all
kernel faults, and polling cannot prove DMA quiescence.

The marker is stored persistently in the desktop user's state directory before
starting the filter. A service crash, kill or failed cleanup therefore blocks
another launch, including after logout/login. Neither a new PipeWire serial nor
a new server is a safe recovery boundary. The service has no automatic restart
or forced-kill recovery. It may remain stuck if the experimental driver retains
resources; stop testing rather than repeatedly restarting services.

A normal service stop interrupts only its own filter, then requires its nodes
to disappear and the same raw source to be idle before clearing the marker.
Close other raw recording clients too before stopping the service. Graph idle
is useful evidence, not proof of electrical power-off or DMA release.

## Device acceptance, after installation is separately approved

Do not use these commands as an instruction to install or test an unqualified
image. Verify the exact image/kernel and preserve access first. Keep speakers
disabled for microphone qualification.

1. Inspect a fresh kernel log and existing audio graph. Do not proceed after a
   PDM/AOP/SmartIO fault, timeout or ownership-retention message. Verify the
   desktop's PipeWire version and that no recording application is active.
2. Start the installed opt-in service once:

   ```sh
   systemctl --user start omarchy-j71b-microphone.service
   systemctl --user status omarchy-j71b-microphone.service
   ```

3. Confirm the named conditioned source is selectable in a normal desktop
   recorder. Confirm the default microphone did not change without user action.
   Select the conditioned source explicitly. Check normal speech, louder speech,
   stereo routing, noise/clipping, start/stop and repeated recordings in the same
   healthy session. Include a mono recorder and ordinary app cancellation.
4. After each recording, verify the raw source returns to idle/suspended and
   collect delayed kernel clock-release evidence. The persistent filter must
   not keep capture active while unused. Suspend/resume is a separate explicit
   device test, not established by software tests or a recording alone.
5. Close all recorders, then stop the service once and verify removal:

   ```sh
   systemctl --user stop omarchy-j71b-microphone.service
   journalctl --user -u omarchy-j71b-microphone.service -b --no-pager
   ```

After a fault or hang, do not retry capture, unload/unbind drivers, or use a
software reboot as recovery. Preserve logs; a physical reset may be needed.
Only after a completed physical reset and inspection of the new boot, the
operator may explicitly acknowledge recovery:

```sh
python3 /usr/lib/omarchy-j71b-microphone/desktop_mic.py --reset-after-physical-reset
```

This only clears the marker when the recorded boot differs. It cannot verify
the physical action and does not start capture. Never delete a marker merely
to bypass a failed test.

## Local Firefox acceptance, without uploads

Firefox is included in the image. For an already-installed, approved candidate,
save `experiments/j71b-capture/browser_test.html` locally on the tablet. This is
only a test page; it does not replace any installed microphone files or change
the fixed gain. Open the saved file from Firefox's **Ctrl+O** dialog, not a hosted
website. No account, local server or new recorder is needed.

In an SSH shell owned by the same desktop user, connect to the existing user
session and start the service once:

```sh
(
  set -eu
  [ "$(id -u)" -ne 0 ] || { echo 'Use the desktop user, not root.' >&2; exit 1; }
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  timeout 20s systemctl --user start omarchy-j71b-microphone.service
  sleep 10
  timeout 5s systemctl --user show omarchy-j71b-microphone.service \
    --property=ActiveState,SubState,Result,MainPID
)
```

Continue only with `active`, `running`, `success` and a nonzero main PID. A failed
or timed-out command is a stop condition, not a reason to repeat the start.
In Firefox, click **Record one 10-second take**, explicitly choose **J71b
Microphone (conditioned)** in the permission prompt, and allow that take. If
the source is absent, cancel. The page refuses a different or missing track
label before creating a recorder. It cannot prevent the browser from briefly
opening a wrongly selected microphone while granting permission.

Keep the tab visible and speak normally. Hiding or closing the page cancels the
take instead of relying on a background tab's timer. The page stops all acquired
tracks after about ten seconds and
shows the actual source label, reported processing settings and capture duration.
It requests echo cancellation, automatic gain control and noise suppression off;
the displayed results matter because browsers can ignore or omit settings. The
page does not change the system default, upload audio or play sound. It offers a
local original browser recording and, when offline decoding works, a PCM16 WAV
without normalization. Neither is bit-exact native PCM. If WAV decoding is
unavailable, keep the original; no recapture is needed for that conversion error.

After downloading the desired file, close the page and stop the service once:

```sh
(
  set -eu
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  timeout 30s systemctl --user stop omarchy-j71b-microphone.service
  timeout 5s systemctl --user show omarchy-j71b-microphone.service \
    --property=ActiveState,SubState,Result,MainPID,ControlPID
)
```

Expected: `inactive`, `dead`, `success` and both PIDs zero. For a failure, keep
the output and the last 30 service journal lines; do not force-kill, restart or
delete a fault marker. Keep playback on a separate computer while speaker
qualification remains open. A successful take establishes this browser path,
not all applications, physical clock/DMA release or suspend/resume safety.

The page's pure mocked tests cover source refusal, cancellation, late permission
resolution, recorder/decoder failure, page dismissal, track cleanup and WAV
encoding. They do not establish Firefox's actual local-file permission behavior.
If Firefox refuses local-file capture, report that result; do not disable browser
security settings or silently switch to an external recording site.

## Build and verification

`system/microphone.py` compiles `experiments/j71b-capture/capture.c` on the native
AArch64 rootfs builder. Compiler/header packages stay in the builder, not the
installed system. A receipt binds the source and plugin hashes. Public-root
assembly rejects missing/stale receipts and non-AArch64 shared objects rather
than silently reusing an older base without the microphone plugin.

Software coverage includes DSP/real SPA graph tests, config-parser isolation,
source identification, route validation, fault-marker durability, the
unexpected-exit/shutdown race, non-restarting cleanup and package verification.
The CI-only harness `experiments/j71b-capture/test_desktop_session.py` exercises
private synthetic PipeWire/WirePlumber sessions and the actual supervisor's
`serve()` and ownership routines on x86_64 and AArch64. It does not call the
hardware-gated production entry point or change its board checks. Check the
current commit's `synthetic-session` jobs before claiming this coverage passed.

The same isolated suite includes the pinned PipeWire PulseAudio bridge and real
`pactl`/`parec` clients. It checks source enumeration, explicit and deliberate
default selection, source indexes and stereo routes, mono/stereo PCM at several
rates, expected +12 dB signal level, idle and teardown. Missing-source and
source-removal cases cover clients that explicitly disable fallback; ordinary
apps may intentionally follow the user's default. This protocol coverage does
not claim a particular browser's permission UI or recording workflow was tested.

The bounded disposable-container test covers recorder formats, repeated and
concurrent consumers, default selection, idle and clean teardown, plus retained
ownership after source/link loss, child exit, server disconnect and a hung
filter. Only diagnostic logs are retained, never audio samples. Forced cleanup
of synthetic fixtures happens after assertions and is not production recovery.
The separate `systemd-lifecycle` CI job uses an already-running, unprivileged
Ubuntu user manager and synthetic processes to verify inactive prerequisites,
healthy stop, retained ownership after faults, no automatic restart and no
forced-kill recovery for hung children or supervisors. It preserves the
production unit and board guards, testing a separate fixture with the same
lifecycle directives. It does not qualify the tablet's exact systemd version,
desktop GUI selection, ARM scheduling deadlines or hardware safety.

The synthetic generator is externally clocked by PipeWire's dummy driver.
In PipeWire 1.6.10, a live `audiotestsrc` acting as its own driver disarms its
timer after buffer starvation and does not restart it when buffers return.
Retained failing CI logs and a socket-free regression against the unchanged
SPA source reproduce this path. The same regression verifies recovery with
an external clock. Exact-name server rules select follower mode for only the
two synthetic sources. No upstream engine or production microphone setting
is patched, and the session tests require the source and clock to become idle.
