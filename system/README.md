# Installed system

One root image for every platform. orchard's platform layer (kernel, DTBs,
modules and orchard's hardware defaults under `/usr/lib`) goes next to it at
install time. This directory is Omarchy A's policy only.

- `packages.txt`: required signed Arch Linux ARM userspace packages; never a kernel package.
- `build-rootfs`, `assemble-rootfs`: produce the clean userspace base and its package snapshot and receipt.
- `overlay/`: power and sleep policy, sudo and polkit rules, backlight permissions, Bluetooth and screen helpers.
- `release/public_root.py`: combine the base, desktop and overlay; explicitly set helper executable modes.

A file under `/etc` overrides orchard's default of the same name under `/usr/lib`.

The installed system boots directly from its boot entry, which orchard's installer writes with orchard's kernel, initrd and the board's DTB; no recovery hands over to it, and the system needs no kernel-install or initrd generator.

The ready helper runs in the greeter's and the tablet's Wayland sessions once their shells are up. The greeter's record, `/run/omarchy-greeter/ready.json` (directory from tmpfiles.d), tells the installer the PIN screen is on; it holds the boot ID, the shell's process and its socket.

The opt-in J71b microphone conditioner is compiled from
`experiments/j71b-capture/capture.c` during base assembly. The native AArch64
builder needs `gcc` and `ladspa` headers; neither is added to the target package
list. `system/microphone.py` records the source and plugin hashes in
`/etc/omarchy/j71b-microphone.json`. Public-root assembly checks that receipt and
the plugin's AArch64 ELF header against the current source, rejecting stale or
missing components in cached bases. This packaging does not enable the user
service; application/device qualification is separate from build validation.
