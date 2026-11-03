# Final J71b power qualification

## Candidate and evidence

- Orchard PR #1: a68aae672613176d203d4a3823f03f929bc84f5c, source tree 2cbfb968f4312ee59fb584613a639cc4a5c730e3.
- Reused full 4K/16K components: Orchard Actions run 41734585711, built from 1b9457c9cc17327ccd08a19a0181c28c6d6028ed. The workflow requires exact source-tree equality to the published commit, component checksums, exact resolved configuration and production power-source receipts.
- Linux production driver: 778174c50d5fd6b36849f9eea728a6811b1b6e9e.
- Omarchy menu PR #2: 2a9462d90c57409173b2d8be7fe998731467ed1c.
- Reused userspace and artwork: Omarchy Actions run 41580024596 at 94e93b8aa393da61a8d6d855f1601382867488fc. The workflow verifies production desktop/system/boot/release sources differ only by the menu fix. It overlays that menu using the existing desktop staging transformation, updates the desktop manifest through the production root assembler, and verifies the finished archive.
- The original rootfs receipt and explicit transformation provenance are retained in qualification-receipt.json. Kernels, desktop binaries and base packages are not rebuilt. Storage-tool hardlinks are normalized using the earlier successful installer procedure.
- No device is accessed by this workflow. This package is not hardware-qualified merely because assembly passes. No release or upstream submission is made.

## Bounded acceptance question

Does the exact final integrated candidate retain the clean candidate's visible shutdown/reboot behavior on J71b, with the final menu fix, while reaching a usable desktop with USB serial and SSH intact?

Use the user's normal established installer/launch procedure only after their explicit approval for that attempt. Preserve the existing working system and microphone work. Do not overwrite, resize, install, or boot a device without approval. One boot attempt per go-ahead, no retry loops. This document is a qualification checklist, not permission to act.

Before testing, record the package SHA256SUMS result, qualification-receipt.json, selected J71b apple-16k layer, installed kernel/DTB hashes, kernel release, running command line, and boot log/dmesg. Confirm the final System menu contains systemctl poweroff and systemctl reboot. Verify the power driver bound and USB serial/SSH work. If source/artifact/runtime identity cannot be established, stop and resolve that gap.

## Four observations

Each row needs a fresh confirmed boot of this candidate. Record connection state, action, UTC time, observed timing, serial/kernel evidence where available, and whether a power-button press occurred.

1. USB connected: use System menu Shutdown. Observe until the screen/backlight is dark and remains dark; record any spontaneous reboot. After the agreed observation window, the user presses power and records Apple-logo and iPadOS behavior.
2. USB connected: use System menu Reboot. Observe whether iPadOS starts automatically without a power-button press.
3. Battery only: disconnect USB after readiness checks, use System menu Shutdown. Record backlight extinction and whether it remains dark. After the agreed observation window, the user presses power and records Apple-logo and iPadOS behavior.
4. Battery only: use System menu Reboot. Observe whether iPadOS starts automatically without a power-button press.

Use the earlier clean-candidate evidence as the comparison: shutdown stayed dark until a button press on USB and battery, and reboot automatically reached iPadOS in both states. The battery shutdown took longer to extinguish the backlight. Choose a finite observation window with the user before the test; report a timeout as inconclusive rather than silently extending it.

## Pass and stop conditions

Pass requires proven candidate identity, usable desktop/USB serial/SSH, corrected menu actions, and all four matching behavioral observations. Any hang, unexpected automatic reboot during shutdown, failure to reboot, loss of access, identity mismatch or need for an unplanned retry stops qualification and requires investigation of the saved evidence. Do not substitute a test on an earlier kernel.

A pass qualifies this bounded user-visible power outcome only. It does not prove electrical-off state, the exact final PMIC execution path, global IRQ exclusivity, real Tristar unbind behavior, or equivalence to Apple's source policy. Keep those limits explicit in PR and tracker updates.

