# J71b capture-conditioning experiment

**Not a tablet fix or an installed profile.** This directory is outside the
system overlay and distribution build. It changes no driver, image, audio
route, speaker setting, service or default volume. No recording or private
calibration is included. Do not copy the experiment into a live PipeWire
configuration as a persistent profile. An explicitly requested, one-session
hardware acceptance procedure is provided in [HARDWARE-TEST.md](HARDWARE-TEST.md).
It is not enabled or installed automatically, and it does not remove the
remaining lifecycle and hardware acceptance requirements below.

This advances the microphone deliverable by testing the **real PipeWire SPA
filter-graph engine** with a candidate that handles startup and reset, rather
than evaluating only filter arithmetic. `spa_graph.py` hosts the installed SPA
engine and LADSPA adapter headlessly. It never connects to a PipeWire server,
audio device or user session. It is a test host, not tablet software.

## Candidate behavior

`capture.c` is a small two-channel LADSPA processing component:

- Independent second-order 80 Hz Butterworth high-pass filters, using double
  state. On activation/reset, each initializes from its first nonzero finite
  input sample. Initial digital-zero buffers do not consume initialization.
- A 20 ms startup fade, restarted on each activation. No channel mixing,
  beamforming, noise suppression or automatic gain control.
- Explicit gain control from 0 to 24 dB, **default 0 dB**, smoothed over 20 ms.
  Tests at 12 and 24 dB establish arithmetic, not a recommended tablet gain.
  A calibrated or broadly tested default has not been established.
- A fixed -1 dBFS emergency output clamp and an exposed overload counter.
  This can distort overloaded signals. It is not a transparent limiter,
  microphone calibration or speaker-protection system. Do not hide overload
  counts when qualifying gain.
- Nonfinite samples emit zero, increment an invalid-input counter and reset
  the affected channel. There is no allocation, I/O or locking in `run()`.
  No hard-real-time capability is claimed without platform qualification.

The first-sample handling assumes activation/reset marks the start of valid
capture. It cannot distinguish a new electrical/DC jump in the middle of a
stream from legitimate audio. That known limitation is tested explicitly.
Do not add a silence heuristic that silently discards speech onsets.

A live source cannot retroactively fade samples already delivered to an
application when that application stops recording. DC rejection removes the
large baseline contribution, but guaranteed click-free arbitrary WAV endings
require recorder fade/tail handling. This experiment makes no such guarantee.

## Reproduce without hardware

Dependencies: a C compiler, `make`, LADSPA development headers, Python 3 with
NumPy, installed `libpipewire-0.3` and SPA filter-graph/builtin/LADSPA modules.
On Debian trixie these are provided by `build-essential`, `ladspa-sdk`,
`python3-numpy`, `libpipewire-0.3-0t64` and `libspa-0.2-modules`.

```sh
cd experiments/j71b-capture
make test
make sanitize
python3 spa_graph.py
```

The graph tests fail when the real engine is absent; they never substitute a
mock. `make sanitize` enables AddressSanitizer, UndefinedBehaviorSanitizer and
leak checking. Some traced/sandboxed hosts cannot run LeakSanitizer; report that
separately rather than declaring the complete sanitizer target passed.

Test coverage includes constant DC of either sign, leading digital silence,
activation/reset/recreation, independent channels, passband gain and 80 Hz
cutoff, block partition invariance, multiple rates, digital overload,
nonfinite recovery and in-place buffers. A stock-filter regression fixture
confirms the original opening DC step, and a midstream-step test preserves
an explicit unimplemented requirement.

The isolated CI workflow records package and PipeWire versions. No recording
or playback on a tablet is part of CI. CI and a headless graph pass do not
establish WirePlumber routing, a real-time deadline, ARM performance, hardware
capture lifecycle or the installed tablet's PipeWire version compatibility.

The upstream CI jobs also build the exact PipeWire 1.6.10 commit on native
x86-64 and ARM64 and repeat the graph tests against that library and its SPA
plugins. They assert the loaded runtime version, not just the checkout tag.
To reproduce with `meson`, `ninja-build` and `pkg-config` installed:

```sh
git clone --depth 1 --branch 1.6.10 https://github.com/PipeWire/pipewire.git build/pipewire-source
bash test_pipewire_1_6.sh build/pipewire-source build/pipewire-build
```

The script checks the immutable commit and rejects tracked source changes.
It never installs PipeWire, starts a daemon, or connects to a sound device.
An upstream 1.6.10 graph pass still does not test a distribution's complete
runtime configuration or capture lifecycle.

Optional configuration output is intentionally inert:

```sh
python3 fixture_config.py build/j71b-capture.so > build/fixture.conf
```

It targets only `j71b.test.fixture`, disables autoconnection, disables fallback,
and leaves gain at 0 dB. Use only with explicitly linked synthetic sources in
an isolated test daemon. The config itself is not exercised by the headless
SPA tests; daemon/stream/routing tests remain a separate prerequisite.

## Acceptance still required before tablet enablement

1. Independent implementation review and sanitizer/graph CI on the candidate.
2. A daemon-level synthetic-source test of routing, missing target, idle,
   resume, repeated recording, configuration and removal. Confirm reset reaches
   the plugin on each relevant transition, and no source fallback occurs.
3. Exact installed PipeWire/SPA version and AArch64 build/runtime validation.
4. J71b-only source identification and reviewed raw-capture ownership/lifecycle.
5. One explicitly authorized controlled device session, keeping raw ALSA
   available for comparison: voice level, start/stop, gain/overload behavior,
   repeated open/close and no unexpected capture while idle. Never automatically
   reopen or unload the experimental capture driver after an ownership fault.
6. Select a gain policy from representative recordings, including louder voice,
   rather than interpreting one quiet sample as calibration.

There is no automatic installation target or image rebuild. Resume the single
project tracker with these remaining gaps; a prototype PR is not completion of
the usable microphone outcome.

## Primary interface references

- [PipeWire filter-chain](https://docs.pipewire.org/page_module_filter_chain.html)
- [SPA filter-graph interface](https://github.com/PipeWire/pipewire/blob/1.4.2/spa/include/spa/filter-graph/filter-graph.h)
- [Stock builtin behavior](https://github.com/PipeWire/pipewire/blob/1.4.2/spa/plugins/filter-graph/builtin_plugin.c)
- [Filter-chain pause/reset handling](https://github.com/PipeWire/pipewire/blob/1.4.2/src/modules/module-filter-chain.c)
- [PipeWire 1.6.10 builtins](https://github.com/PipeWire/pipewire/blob/f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171/spa/plugins/filter-graph/plugin_builtin.c)
- [PipeWire 1.6.10 pause/reset handling](https://github.com/PipeWire/pipewire/blob/f9d6cbc99c354b9b0ad1d37245dc3ab0fb9f9171/src/modules/module-filter-chain.c)

The locally exercised engine version must be reported explicitly. A current
web documentation version does not prove compatibility with that engine or
the tablet. Source inspection alone does not substitute for running a version.
The stock ramp fixture has different failures in the two tested runtimes:
1.4.2 does not restart its fade on graph reset. In upstream 1.6.10, JSON
controls are clamped to declared ranges; the ramp's zero-valued ranges clamp
`Stop=1` and `Duration=.02` to zero and mute the fixture. This is not evidence
of a corrected fade. The version-specific negative control records both
behaviors; the candidate's nonzero AC, gain, envelope and reset assertions are
identical across runtimes and do not use the stock ramp.
