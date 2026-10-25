# A30s eMMC root bring-up

## Current state

The current SWEET ADT deliberately has no storage node and the current
payload tool only describes an in-memory `RAMDisk` (`rd=md0`). The generic
SWEET root driver is for Qualcomm UFS and cannot be reused as a host driver
for this phone. The A30s uses the Exynos 7904 DW-MMC controller at physical
`0x13500000`; the Samsung DTS describes one non-removable, 8-bit eMMC slot
and enables DDR, HS200 and HS400 modes. The board clock/pin/timing setup in
Linux is substantially more involved than the generic DesignWare register
block.

## Safe implementation sequence

1. Add an opt-in, read-only XNU eMMC probe. It must validate the live host
   state and report controller registers/card identity without resetting
   the controller, changing clock timing, or writing to media.
2. Add polled block reads only after the probe and DMA/cache requirements
   are understood. Keep writes unavailable by construction.
3. Locate a named GPT partition and expose it as a distinct read-only BSD
   block device. Root selection must remain opt-in (`rd=...`) and fail
   clearly if the controller/card/partition is unavailable.
4. Build a host-side root image from the local iPhone X IPSW and validate
   its filesystem offline. This image is an artifact only; it is not to be
   written to a phone during development.
5. Require CI kernel build, LSE scan, QEMU regression checks, and source
   review before considering hardware testing. No phone test is part of
   this work without separate human approval.

## Known limitations

The boot path has not yet measured the DW-MMC state handed off by SBOOT.
The Linux driver depends on Exynos CMU clocks, pinctrl, voltage/timing
configuration, and DMA/cache handling. The generic controller address and
Linux source alone do not prove the host remains initialized at XNU entry.
Do not advertise a `userdata` image as bootable until XNU can read the
target partition and the complete root has been mounted successfully.
