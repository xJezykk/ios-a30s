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

The generated A30s ADT now describes the Exynos 7904 DW-MMC0 host at
`/arm-io/dwmmc0`, with the SOC-relative register and CMDQ windows, interrupt,
and non-removable 8-bit slot. Patch 0086 retains the opt-in, read-only
register probe; patch 0088 replaces it with an experimental polled PIO eMMC
block device and routes `rd=emmcroot0` to a GPT partition (`emmcroot=`,
default `userdata`). Writes are disabled unless `emmcw=1` is supplied and
are bounded to that selected partition. The driver has not been compiled
against the complete XNU build environment or tested on the phone. The
payload tool still describes an in-memory `RAMDisk` (`rd=md0`), and the
experimental driver does not make an iPhone X IPSW root filesystem usable
on the A30s.

## Safe implementation sequence

1. Establish that patch 0088 applies after patches 0086 and 0087 and builds
   with the pinned XNU source and its supported toolchain. Resolve any
   compile-time API or locking issues before attempting hardware access.
2. Keep hardware testing opt-in. First test controller initialization and
   read-only access to a known-safe partition; do not enable `emmcw=1` on
   Android `userdata`. If the bootloader handoff or card state differs from
   assumptions, stop rather than repeatedly resetting or retraining it.
3. Before attempting any root-image work, establish which IPSW files are
   usable by the experimental XNU port. An iPhone X IPSW root filesystem is
   not presumed mountable or executable on the A30s. Any extracted image is
   a host-only artifact until its format, compatibility, and size are proven.
4. Require CI kernel build, LSE scan, QEMU regression checks, and source
   review before considering hardware testing. No phone test is part of
   this work without separate human approval.

## Known limitations

The shim has measured controller registers, including the A30s `CLKSEL`
value, but has not established card protocol state or proven an XNU block
read. The experimental driver assumes the live Exynos clock source and
programs legacy SDR timing; the source-derived clock rate and its suitability
at XNU entry remain unverified. It uses PIO, but still needs a supported
build, hardware validation, and confirmation that the selected root partition
contains a compatible, bootable filesystem. Do not treat `userdata` as a
safe write target or advertise it as bootable before those conditions hold.
