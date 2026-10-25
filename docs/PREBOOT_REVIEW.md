# A30s pre-boot review

**Review date:** 2026-10-25  
**Original pre-test decision:** NO-GO.

Status update: the user reports that the 4 KiB candidate was booted on the
physical A30s and displayed `SWEET AMFI: built-in interface registered`, then
stopped producing console output. This report has not been independently
verified; no device-side image hash, register state, or kernel log was
collected here. The review below records the evidence available before that
reported test. No phone interaction occurred during the follow-up analysis,
and no eMMC probe was enabled by this work.

## Branch and image candidates

The checked-out branch is `fix/a30s-xnu-ci`; local `HEAD` and its tracking
ref are both `ee6552674b8c29a0195165a23c4ee87b5f7b4bed`.

The earlier candidate files remain unchanged. The `6fc2b13` image is not
proven to descend from this branch, while the earlier `96aad960` image has
`emmcprobe=1` enabled:

| Image | Size | SHA-256 | Provenance and suitability |
| --- | ---: | --- | --- |
| `a30s-first-boot-candidate-6fc2b13/a30s-xnu-6fc2b13-p2.img` | 16,631,808 bytes | `74c301b67c0337c0d5a073222cab6fdc9cb4aaf5da552cb18df164745926b6eb` | Candidate record reports a 4 KiB TCR kernel and patches 0082–0085; its payload command line is `debug=0xc serial=3 keepsyms=1 cpus=1`, without `emmcprobe=1`. Its provenance names repository commit `6fc2b13d95413c01740c929e9015ddf53026fa4b`. That Git object is not present locally, so its ancestry to `ee655267...` is unverified. Its own status says hardware was not tested. **Not approved for use.** |
| `a30s-emmc-probe-boot-96aad960/a30s-emmc-probe.img` | 16,631,808 bytes | `16a149706e1f904719ca223d6d2696d19a979985157ec2bc9743e68bd0d64edc` | Candidate record identifies Actions merge commit `96aad960f7d15992ba366a4be597524f4c31de17` as merging `ee655267...` into `ca7b4d3...`; its payload explicitly has `emmcprobe=1`. The merge object is not in the local Git database, so I could not independently verify ancestry. **Reject for this test because it enables the probe.** |

The image and component manifests for both earlier directories pass
`sha256sum -c`. The old eMMC artifact's `BUILD-PROVENANCE.txt` records patch
0084 only, while its CI log shows patches 0082–0086 applied. The new
candidate manifest records and explains that discrepancy.

## New probe-disabled candidate

Created host-side at `../a30s-physical-candidate-ee655267-no-probe-v1/` from
the exact branch source tree. The image is
`a30s-xnu-ee655267-no-probe.img`, **16,631,808 bytes**, SHA-256
`f1b96431be8e97c80ac54e064bbf2768ab9d7c26845b559880a6a86fb0e7d476`.

The XNU kernel came from the saved Actions artifact built at merge commit
`96aad960f7d15992ba366a4be597524f4c31de17`. Its checkout log explicitly
records merging `ee655267...` into `ca7b4d3...`; local Git confirms `ca7b4d3`
is an ancestor of `ee655267`, so the merged source tree is the branch-tip
tree. The merge object itself is not in the local Git database. XNU was not
recompiled on this Linux host; the host packaged the CI kernel using A30s
builder sources unchanged from `ee655267...`.

The CI log records 0083 (4 KiB pages) and 0086 (probe support) applied. The
CI build-tree copy of `SWEET.h` omits the active 16 KiB macro, and
`proc_reg.h` selects `ARM_PGSHIFT=12` and 4 KiB TCR granules. Independently,
the final-image verifier parsed the produced boot image, matched its shim and
embedded payload, checked the Mach-O segment map and ADT, decoded the actual
kernel boot TCR as `T0SZ=25/TG0=4KiB, T1SZ=25/TG1=4KiB`, and found no
`emmcprobe` in either the payload or outer boot-image command line. The shim
copies the payload command line into XNU boot args. Probe code is compiled in
but requires explicit `emmcprobe=1`, which is absent. See the candidate's
`BUILD-MANIFEST.txt`, `PAGE-CONFIG-VALIDATION.txt`, and
`independent-validation.log` for full evidence.

The non-probe 6fc image is Android boot image v1 with 2048-byte pages,
kernel address `0x10008000`, a 96-byte ramdisk, and outer boot-image command
line `console=ram`. Its embedded SWEET payload has the separate command line
shown above. The wrapper is 16.6 MB; the target boot partition was documented
as 37,748,736 bytes, but its live size/mapping was not checked in this review.

## Backup and restore coverage

`backup_samsung/boot.img` exists, is 37,748,736 bytes, and parses as an
Android boot image. Its MD5 is `7e5c1f459204da19c12d81a267b3e1cd`, matching
the value recorded in `NOTES.md`; its SHA-256 is
`1544c4f96a31b48ea7f307e7c2ac52f24cb0255295653de54a8f5ee360aef028`.
The note identifies it as the Magisk-patched boot captured on 2026-10-23,
not day-one stock firmware.

This verifies the host file against the historical digest. It does **not**
prove that the backup matches the phone's present boot partition. Later notes
record multiple writes to boot and a pre-test live image whose hash matched no
known artifact. No current on-device hash was obtained. The backup could
restore the captured boot-partition contents if the target mapping is still
correct; it is not a complete backup of the phone's original configuration
or current state. `recovery.img` also exists (55,574,528 bytes), but no
recorded reference digest was found to verify its integrity. Neither backup
was modified.

## Documented physical procedure and writes

`NOTES.md` describes Samsung SBOOT and Download Mode, says generic fastboot is
not available, and records the target as `/dev/block/mmcblk0p15` for the boot
partition. The historical flash plan is to stage the candidate and boot
backup, write the candidate to that partition with root `dd`, read it back
and compare hashes, then reboot. A later entry says TWRP was used for the P2.6
write/readback and that `/data/local/tmp` does not exist in TWRP; it used
TWRP's `/tmp` instead. The older plan says to push files to SD but shows a
`/data/local/tmp` source path. These are not a single internally consistent,
ready-to-run command sequence.

The persistent changes that procedure intends are:

1. Staging creates a file on SD (persistent removable storage) or in Android
   `/data/local/tmp` (userdata). The TWRP `/tmp` alternative is temporary
   recovery RAM storage.
2. `dd` writes the candidate bytes to boot partition `mmcblk0p15`.
3. Reboot resets volatile runtime state and starts the newly written boot
   image. The later restore procedure would write the saved boot image back to
   the same partition and reboot again.

The notes say not to write `recovery`, `param`, `efs`, or `radio`; none is an
intended target. Hash readback is a read, not a write. The exact p15 mapping
and partition size still need confirmation for the actual SM-A307 variant.
No repository evidence proves that the candidate or kernel has zero other
hardware side effects; “only boot partition” describes the planned persistent
write, not a guarantee about all device state.

## Recovery if startup fails

The notes report that TWRP could write and read back the P2.6 image on
2026-10-24, and describe OrangeFox as having its own kernel. This is evidence
that recovery tools worked during that historical session, not proof that
recovery is currently installed, reachable, or usable after a hang. The repo
does not establish the exact current key sequence, USB/download-mode access,
ADB availability in recovery, bootloader state, or a tested restoration of
`backup_samsung/boot.img` after failure. It therefore does not establish a
reliable recovery guarantee for a blank screen or a kernel hang.

The host backup is available, but recovery requires a working path back into
SBOOT/Download Mode or a functioning recovery environment, correct device
mapping, staging access, and a verified restore write/readback. Those are
prerequisites, not proven fallbacks. Do not rely on a blind reboot or assume
that the recovery backup can be flashed safely.

## Non-writing boot method

No genuinely non-writing boot method is verified for this phone and firmware.
The project notes explicitly say Samsung SBOOT/Download Mode, not fastboot.
The only `fastboot boot` mention is a comment in the log-pull script, not a
device test or firmware capability check. Historical physical tests wrote
the boot partition. No in-RAM loader or verified one-time boot command is
documented. A non-writing method must not be assumed.

## Signals and evidence for a future approved test

If a future test is separately approved, the expected **staged** screen
signals from the shim/XNU path include the shim banner, `XNU jumping`, SWEET
framebuffer/platform-expert initialization, `VM bootstrap done`, and AMFI
registration. These are progress markers, not a complete boot. The current
image has no root filesystem or intended userspace; reaching AMFI alone is
not a successful OS boot.

Failure signals include no shim output, a blank screen, a freeze at `XNU
jumping`, an early panic/exception, an unexpected reboot, or failure to reach
the next known milestone. The prior 16 KiB P2.6 image reached `__start` and
froze before the console; it is not the current 4 KiB kernel.

Evidence to capture would include exact device model/firmware and bootloader
state; candidate and backup hashes; pre-write and post-write boot-partition
readback hashes; timestamped screen photos/video at each stage; any available
serial/ADB/recovery logs; whether the phone remains powered or reboots; and
restore readback evidence. Do not use QEMU logs as physical-device evidence.

## Go/no-go

**NO-GO for another physical boot attempt at present.** The user-reported
AMFI line is meaningful early-kernel progress, but it does not establish that
startup continued, that the console stopped because the kernel halted, or
that eMMC/root/userspace works. The current boot partition hash, exact phone
variant, partition mapping, recovery route, backup-to-current-state
correspondence, and restoration capability remain unverified. There is no
proven non-writing boot method.

The next diagnostic should be host-side QEMU tracing with explicit phase
markers and repeated stopped-state register/stack snapshots. Before any later
device change, confirm the exact phone/firmware and current image identity,
establish a read-only boot-partition hash, verify a restorable backup, and
establish recovery access and a credible restore procedure. Any future
partition write or reboot requires separate explicit approval.
