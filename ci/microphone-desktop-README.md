# J71b microphone-only desktop test

For iPad 6 Wi-Fi: iPad7,5 / J71b / A1893 / T8010 only. This is an experimental desktop installation, not a completed audio release. It replaces the disposable Linux installation through the normal installer. Do not install it on another model. The standard apple-16k manifests can describe other boards; the installer does not itself enforce this J71b-only testing restriction.

## What is included

- Linux c410dc9c3f61fa12ca54c8fbf77f3e182e4e8d5f, based on audio candidate 0bdf444d56aad6f99616dc739f0a18e8937694b5.
- The microphone implementation tested in RAM capture r3, including its AOP reply correction and native live calibration reader.
- Upstream-based MCA FIFO error reporting. This does not claim to fix speaker noise or microphone gain/DC offset.
- A test-only J71b device-tree gate disables the speaker card and MCA2 before the desktop starts. The existing amplifier codec can still probe and request its normal GPIO-low state. This software gate is not proof of physical amplifier isolation.
- The existing desktop/root filesystem and unchanged boot tools are reused. No new processing or volume profile is included. Exact build provenance and corresponding sources accompany the archive.

Apple firmware remains private and is obtained by the normal installer. No firmware or device recordings are published. There is no scripted recording or playback test in this package.

## Important limitation

The AOP host-ownership assumption is experimental. A firmware or transport failure may leave capture close, suspend or shutdown waiting indefinitely to avoid unsafe DMA resource release. A physical reset may be required. A command timeout cannot reliably terminate a process blocked in kernel teardown. Do not retry capture, unbind drivers or unload modules after a hang.

The earlier r3 test completed one ten-second recording. Voice was audible but quiet, and startup settling/DC offset remain under investigation. This desktop package has not yet been tested on hardware.

## Before installation

Extract the complete artifact ZIP into its own Downloads folder named omarchy-j71b-microphone-desktop-assets. Do not mix it with the RAM packages or the power-off diagnostic installer.

From Mac Terminal:

    cd "$HOME/Downloads/omarchy-j71b-microphone-desktop-assets"
    shasum -a 256 -c SHA256SUMS
    cat test-kernel-commit.txt

The revision must match c410dc9c3f61fa12ca54c8fbf77f3e182e4e8d5f. Stop if any checksum fails. When you choose to install, use the included install.sh and the normal README/DFU flow. No kernel or desktop build runs on the Mac. Do not choose Restore in Finder.

## Initial desktop check

Reconnect SSH from Mac Terminal after installation; an earlier connection does not survive a reboot. If the host key changed after reinstall, verify the new device identity before replacing the old entry.

Inside the iPad SSH session, first inspect without starting another recording:

    uname -r
    cat /proc/asound/cards
    cat /proc/asound/pcm
    sudo journalctl -k -b --no-pager | grep -Ei 'pdm|aop|sio|snd|audio|firmware'
    wpctl status

Expect the microphone card and capture PCM. The J71b speaker card must be absent. Stop if it is present or any PDM/AOP/SmartIO fault appears. Share the output before choosing the next controlled desktop recording test. Do not use this build for a speaker playback test.
