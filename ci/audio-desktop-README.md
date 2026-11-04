# J71b current audio integration desktop test

For iPad 6 Wi-Fi, iPad7,5 / J71b / A1893 / T8010 only. Experimental, not hardware validated. This package replaces the disposable Linux installation through the standard installer. Do not install on another model. Manifests may describe other apple-16k boards; this restriction is not enforced by the installer.

## Defined question

Can the audio integration on its pinned pre-power-merge baseline reach the desktop, enumerate the capture card, and retain working USB serial/SSH while the speaker card and MCA2 remain disabled? A subsequent explicitly approved recording can investigate capture quality. This package starts no recording and performs no playback.

## Contents and provenance

This variant is pinned to Orchard `79df400f60c76154a47fdb4dc1eff06dbb1aa8a2`, before the power PR #64 merge. It is not latest main and does not include the later `7e686b3` power integration. It has not been rebased onto that change. The Linux audio source remains `6af872e1ca0ecfd8eb0c6b5f21bed1c964d45b48`; the workflow runtime repair does not change that kernel source.

The expected kernel release is exactly `7.3.0-rc1-j71b-audio-nov4-r1-orchard-apple-16k`. Exact Linux and Orchard commits and GitHub hardware artifact pins are in `audio-desktop-inputs.json`; `integration-receipt.json` and `reuse-receipt.json` document source integration and reused tools. Current TDM driver source is retained; the J71b `/sound` and MCA2 device-tree nodes are disabled. This software playback gate is not proof of physical amplifier isolation. No D2365 stack, Apple-derived alternative, volume profile, or processing profile is added.

The previous PR #5 desktop root archive and its receipt are reused byte-for-byte from CI 41717404430, artifact 13272170021. `rootfs-reuse-proof.json` records matching input/output SHA256 values. Its OS version remains the previous desktop version; the preview version identifies this new kernel/platform packaging. `reused-desktop-source.tar.gz` preserves the corresponding desktop source, while `omarchy-linux-source.tar.gz` is this packaging source. Kernel and boot-tool sources accompany the image. Apple firmware, calibration, recordings, raw ADT and identifiers are not included; the standard installer obtains private firmware locally.

## Verify before any installation

Extract the complete artifact ZIP to a fresh folder, without mixing older RAM or power-off diagnostic packages. From that folder on the host run:

    shasum -a 256 -c SHA256SUMS
    cat audio-desktop-inputs.json
    cat rootfs-reuse-proof.json

Stop on any checksum failure or unresolved pin. No device action is authorized by downloading or verifying this package. When you explicitly choose an installation attempt, use the included `install.sh` and its normal README/DFU flow. No source build runs on the host. Do not choose Restore in Finder.

## Initial read-only device check, after an approved install

Reconnect SSH; a previous session does not survive reboot. If the host key changes, verify the device identity before replacing the old key. Inspect:

    uname -r
    cat /proc/asound/cards
    cat /proc/asound/pcm
    sudo journalctl -k -b --no-pager | grep -Ei 'pdm|aop|sio|snd|audio|firmware'
    wpctl status

The full kernel release must match, the capture PCM must exist, and the J71b speaker card must be absent. Stop on missing capture, a speaker card, firmware errors or PDM/AOP/SmartIO faults. Preserve the log before a separate, explicitly approved capture attempt. Do not play audio, change the gate, unbind drivers or unload modules.

## Recovery and stopping condition

AOP ownership and failure handling remain experimental. Capture close, suspend or shutdown can wait indefinitely to avoid freeing DMA resources while firmware ownership is uncertain. A command timeout cannot guarantee recovery from blocked kernel teardown. Stop after a fault or hang, do not loop retries, and use the previously verified physical-reset/recovery procedure. Retain the last known-good installer and host firmware cache. Boot/install, physical reset and any capture attempt require separate approval. Passing CI proves assembly integrity, not device functionality or speaker safety.
