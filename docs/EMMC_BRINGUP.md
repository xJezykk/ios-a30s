# A30s eMMC root bring-up

## Verified host-side baseline (2026-10-25)

- The local IPSW is `iPhone10,3,iPhone10,6_16.7.16_20H392_Restore.ipsw`:
  iOS 16.7.16, build 20H392, for Apple iPhone X product types `iPhone10,3`
  and `iPhone10,6` (D22AP/D221AP). The archive is 6,171,035,648 bytes.
- Its manifest names an Apple `KernelCache`, Apple device trees, an iBoot
  image, restore ramdisk, SEP firmware, and trust caches. The kernel cache and
  firmware entries are IM4P payloads; this inspection read the manifest and
  container headers only. Payload extraction/decryption and the DMG internal
  formats remain uninspected.
- The IPSW is not a generic ARM64 boot image. Its kernel and device tree are
  for iPhone X hardware built around Apple A11, with Apple boot firmware and
  SEP components. The A30s uses Exynos 7904 and Samsung SBOOT. The current
  project boot chain loads a separately source-built XNU `SWEET` kernel via
  `shim/loader.c`; it does not load the IPSW kernel or boot an Apple root
  filesystem. Running this IPSW would require an extensive Apple platform
  port, including compatible kernel/platform drivers, firmware and security
  services, and the userspace boot environment. No compatible iOS boot path
  is established.
- The host has ample space for analysis (`df` reported 617 GiB available on
  the repository filesystem). The phone was not reachable over ADB in this
  session, so its userdata partition size, filesystem free space, and backup
  integrity are not verified here. No IPSW contents were copied to the phone.
- CI artifact `a30s-emmc-probe-boot-96aad960` records a successful XNU build
  of the `ee655267` eMMC-driver commit. The saved Actions log includes
  `sweet_emmc.o` in the kernel object list. The candidate is explicitly
  unflashed; this verifies compilation and packaging, not hardware access.
- The latest recorded phone experiment is the 2026-10-24 P2.6 instrument
  round in `NOTES.md`: the shim reached XNU `__start`, then stopped before
  the kernel console. That test used the then-current 16 KiB-page kernel,
  which the notes identify as incompatible with this ARMv8.0 Cortex-A53
  boot core. The later 4 KiB kernel build and eMMC probe have no recorded
  physical-phone result.

## Current state

The generated A30s ADT now includes `/sweet-emmc`, describing the Exynos
7904 DW-MMC controller at physical `0x13500000` and a non-removable, 8-bit
slot. XNU patch 0086 adds an opt-in, read-only register snapshot. The
payload tool still only describes an in-memory `RAMDisk` (`rd=md0`); no
physical eMMC block device or partition reader exists. The generic SWEET
root driver is for Qualcomm UFS and cannot be reused as this phone's host
driver. The Samsung DTS enables DDR, HS200 and HS400 modes, but its board
clock, pin, voltage, and timing setup is not yet implemented in XNU.

## Safe implementation sequence

1. Add an opt-in, read-only XNU eMMC probe. It must validate the live host
   state and report controller registers/card identity without resetting
   the controller, changing clock timing, or writing to media.
2. Add polled block reads only after the probe and DMA/cache requirements
   are understood. Keep writes unavailable by construction.
3. Locate a named GPT partition and expose it as a distinct read-only BSD
   block device. Root selection must remain opt-in (`rd=...`) and fail
   clearly if the controller/card/partition is unavailable.
4. Before attempting any root-image work, establish which IPSW files are
   usable by the experimental XNU port. An iPhone X IPSW root filesystem is
   not presumed mountable or executable on the A30s. Any extracted image is
   a host-only artifact until its format, compatibility, and size are proven.
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
