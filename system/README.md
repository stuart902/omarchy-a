# Installed system

One root image for every platform. orchard's platform layer (kernel, DTBs,
modules and orchard's hardware defaults under `/usr/lib`) goes next to it at
install time. This directory is Omarchy A's policy only.

- `packages.txt`: required signed Arch Linux ARM userspace packages; never a kernel package.
- `build-rootfs`, `assemble-rootfs`: produce the clean userspace base and its package snapshot and receipt.
- `configure-omarchy-repository`: append the stable Linux ARM64 Omarchy repository after Arch Linux ARM and install its current signed keyring.
- `overlay/`: power and sleep policy, sudo and polkit rules, backlight permissions, Bluetooth and screen helpers.
- `release/public_root.py`: combine the base, desktop and overlay; explicitly set helper executable modes.

A file under `/etc` overrides orchard's default of the same name under `/usr/lib`.

AI application packages follow the current OPR stable channel; their versions
are not pinned. A fresh base build installs the current `omarchy-keyring` and
records its version in `packages.lock`. A reused base keeps that recorded
snapshot; ordinary signed pacman updates can update the keyring and apps.
The build-only `omarchy-pkgs.asc` identifies the same reviewed publisher as the
existing Omacalc input. Only the publisher fingerprint is fixed. The builder
verifies packages in a temporary keyring, while each device initializes its own
keyring and populates both `archlinuxarm` and `omarchy` before graphical login.
Package signatures are required and must be trusted. Repository database
signatures retain Arch Linux ARM's existing optional policy; OPR currently does
not publish a detached database signature.

On a freshly prepared tablet, inspect the source and download the current
packages with their dependencies, without installing them:

```sh
pacman-conf --repo-list
pacman-conf --repo omarchy Server
sudo pacman -Syu --downloadonly --needed \
  openai-codex-desktop claude-desktop grok-bot hermes-desktop \
  lmstudio-bin ollama openclaw perplexity
```

The source must resolve to `https://pkgs.omarchy.org/stable/aarch64`, after
`core`, `extra`, `alarm` and `aur`. Record resolved versions, sources,
architectures and signature results when validating a release. Hermes must be
the Nous Research agent, not the unrelated same-name speech-to-text package.
These are Linux ARM64 packages, not macOS Apple Silicon applications. Download
and signature checks do not prove that the payload runs on an Apple A-series
device, including its 16 KB page-size kernel. Package installation, launch
handoff and verified first launch require separate results. T3 Code's AppImage
source and application-specific setup/repair remain separate work.

The installed system boots directly from its boot entry, which orchard's installer writes with orchard's kernel, initrd and the board's DTB; no recovery hands over to it, and the system needs no kernel-install or initrd generator.

The ready helper runs in the greeter's and the tablet's Wayland sessions once their shells are up. The greeter's record, `/run/omarchy-greeter/ready.json` (directory from tmpfiles.d), tells the installer the PIN screen is on; it holds the boot ID, the shell's process and its socket.
