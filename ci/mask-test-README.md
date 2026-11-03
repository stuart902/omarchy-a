# J71b mask-readback diagnostic installer

For iPad 6 Wi-Fi, iPad7,5 / J71b / A1893 / T8010 only. This is a temporary diagnostic desktop installation, not a production release or a microphone test.

The kernel is 2a38ac557f9ce63580d473e860ffdf77e879ec6d. The diagnostic observes mask-register readbacks after existing normal-operation writes on J71b. It performs no extra mask writes, adds no reads of the shared clear-on-read event bank, and adds no observations to shutdown, suspend or resume. It records one probe bulk write and at most sixteen normal awake byte-write observations. Readback errors stop further observations. Extra I2C traffic and logging can perturb timing. Existing transport errors and hardware timing can still affect the result; an equal readback proves neither interrupt masking effectiveness nor electrical power-off.

This installer reuses the previous verified desktop/base filesystem and unchanged boot tools. Standard assembly includes the current menu shutdown/reboot fix. The test kernel and installer have separate CI provenance. Apple firmware remains private and is obtained by the normal installer.

## Before installation

Keep this artifact in a distinct folder, separate from the earlier comparison and microphone packages. Extract the whole downloaded artifact ZIP together. Verify its SHA256SUMS from Mac Terminal in that extracted folder:

    shasum -a 256 -c SHA256SUMS

Check test-kernel-commit.txt equals the full revision above. Use the normal included install.sh only when ready for the explicitly requested installation. It replaces the disposable Linux installation; do not choose Restore in Finder. No kernel or desktop compilation runs on the Mac.

## After boot

Keep the USB/power connection stable while the diagnostic boots. Reconnect SSH from the Mac; an earlier SSH session does not survive reboot. A reinstallation may change the host key, which must be handled as the normal verified-device reconnect rather than bypassing verification blindly.

Inside the iPad SSH session, collect:

    uname -r
    sudo journalctl -k -b --no-pager | grep -F 'mask-test'

Send all matching lines, including errors or absence of output. Do not write mask registers, unload/rebind the GPIO driver, or repeat boots to seek a different result. No shutdown test is required merely to gather this diagnostic. Review the observations before the next hardware action.

The clean driver remains on its separate branch. Do not submit this test-only configuration or diagnostics upstream as the finished fix.
