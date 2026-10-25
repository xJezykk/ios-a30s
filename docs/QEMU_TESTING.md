# A30s XNU testing in QEMU

## What this test covers

The A30s XNU build can run on QEMU's ARM64 `virt` machine when the test
supplies an A30s-derived Apple Device Tree with QEMU's GICv2 addresses and the
QEMU vCPU's MPIDR. Use `cortex-a53` to exercise the ARMv8.0 page-table path
required by the Exynos 7904's A53 cores. The verified setup uses QEMU TCG,
one vCPU, 4 GiB RAM, and `virt,gic-version=2`.

The `virt` machine does **not** emulate an Exynos 7904, Samsung SBOOT, the
A30s display controller, or the physical DW-MMC/eMMC. The framebuffer is
ordinary QEMU RAM. The eMMC probe argument must be omitted; this test cannot
validate controller handoff or block reads.

QEMU evidence is limited to the stages actually seen in the output:

1. QEMU starts the ARM64 guest.
2. The direct-entry stub or A30s shim transfers control to the XNU entry.
3. The XNU framebuffer console appears and kernel services progress.
4. No physical hardware, root disk, eMMC, or iOS userspace is tested.

## Dependencies

Check the local tools and the A53 CPU model:

```sh
python3 tools/qemu-sweet.py --check-env --cpu cortex-a53
```

The test needs `qemu-system-aarch64`, `llvm-nm`, and the Python standard
library. The QEMU run uses the `virt` machine and does not need UEFI firmware:
the harness loads the test memory image and a small entry stub with QEMU's
loader device. If QEMU is missing on Arch/CachyOS, the package command is:

```sh
sudo pacman -S qemu-system-aarch64 llvm
```

Do not install packages just to run this test if the dependencies are already
available.

## Build a QEMU-specific payload

Use a kernel downloaded from the intended GitHub Actions run. Check its
`BUILD-PROVENANCE.txt` and `SHA256SUMS`; do not rely on an older output merely
because its filename looks current. Then generate a separate QEMU ADT and
payload under a temporary directory:

```sh
KERNEL=/path/to/kernel.development.sweet
TMP=$(mktemp -d)

python3 tools/mkadt-a30s.py "$TMP/a30s-qemu.adt" --qemu
python3 tools/mkpayload.py "$KERNEL" "$TMP/a30s-qemu.adt" \
  "$TMP/a30s-qemu.payload" \
  --cmdline 'debug=0xc serial=3 keepsyms=1 cpus=1'
sha256sum "$KERNEL" "$TMP/a30s-qemu.adt" "$TMP/a30s-qemu.payload"
```

The QEMU ADT changes the GIC addresses and boot CPU ID for `virt`. The
command line deliberately leaves out `emmcprobe=1`, since `0x13500000` is an
A30s controller address and not a QEMU eMMC device.

## Boot XNU directly

This uses a 6-instruction QEMU loader stub to pass the XNU boot-args pointer
and branch to the Mach-O entry point. It bypasses the Samsung shim, so it
isolates XNU startup from the phone boot wrapper:

```sh
python3 tools/qemu-sweet.py "$TMP/a30s-qemu.payload" "$TMP/direct-a53" \
  --cpu cortex-a53 --gicv2 --qmp-stdio --secs 45 \
  --kernel "$KERNEL" --a30s-fb --fail-on-panic
```

`--a30s-fb` captures and decodes the A30s ADT framebuffer at `0xEC000000`
(720×1560). `--qmp-stdio` avoids the local Unix socket used by default and is
useful in restricted Linux environments. The output directory contains
`adtfb.txt`, `adtfb.png`, `regs.txt`, `qemu.log`, `serial.txt`, `result.json`,
and `run-metadata.json` with the command, hashes, VM state, stop reason, and
harness exit code. Repeat `--expect-text` for required console markers and
add `--fail-on-missing-text` to return 4 if one is absent. Marker checks do
not claim a full boot. The console result is a framebuffer snapshot; it is
not a complete persistent kernel log.

## Test the shim handoff

To exercise the real A30s shim with the QEMU ADT, build its embedded payload
in a temporary source copy. This avoids replacing anything in `shim/out/`:

```sh
mkdir "$TMP/shim-src"
cp shim/*.c shim/*.h shim/*.S shim/*.ld shim/build.sh "$TMP/shim-src/"
bash "$TMP/shim-src/build.sh" "$TMP/a30s-qemu.payload" "$TMP/shim-qemu.img"

python3 tools/qemu-sweet.py --shim "$TMP/shim-src/out/Image-payload" \
  "$TMP/shim-a53" --sboot-state --cpu cortex-a53 --gicv2 --qmp-stdio \
  --secs 45 --kernel "$KERNEL" --a30s-fb --fail-on-panic
```

`--sboot-state` enters the shim with MMU/cache state and a dirty TLB resembling
the hostile state modeled for SBOOT. This remains a QEMU model; it does not
prove Samsung firmware behavior.

## Recorded QEMU evidence and limits

Fresh 30-second runs on 2026-10-25 used QEMU 11.1.2, explicit TCG, one
`cortex-a53` CPU, 4 GiB RAM, and `virt,gic-version=2`. Both runs were paused
for a snapshot at the requested timeout, then the host harness sent SIGKILL;
this is neither a guest shutdown nor a test of long-duration reliability.
`result.json` says `no_panic_observed` only as a snapshot classification and
sets `full_boot_verified` to false. Both requested text markers were present.

The direct-entry console showed VM bootstrap, OSLog setup, scheduler quantum
configuration, and `SWEET AMFI: built-in interface registered`. The shim
SBOOT-state run reached its shim banner and the same AMFI registration line.
Both QEMU logs are 868 bytes of the known GICv2 `Bad offset a4..bf` writes;
serial output is empty. Full CPU registers and the console snapshot are saved
alongside each log. Compact, hash-checked evidence is in
`docs/evidence/qemu-2026-10-25/`.

### AMFI message and stopped-state follow-up

The AMFI line is printed at the end of `sweet_amfi_register()` after
`amfi_interface_register()` returns. It is a `STARTUP(EARLY_BOOT,
STARTUP_RANK_MIDDLE, ...)` callback, not the end of XNU initialization. The
startup dispatcher then runs the remaining EARLY_BOOT callbacks; after that
phase `kernel_bootstrap()` enables interrupts, initializes the screen and
commpage, proceeds through lockdown, starts scheduler services, and calls
`bsd_init()`. The precise next callback depends on the linked startup table;
the console line alone does not identify it.

A new 30-second snapshot used the same CI kernel SHA above and a QEMU-specific
ADT (`mkadt-a30s.py --qemu`) on `virt,gic-version=2`, TCG, `cortex-a53`, one
CPU. It reached the same AMFI line. QMP stopped the guest at the timeout and
the harness killed QEMU after saving registers and `x/16gx $sp`; the result
remains snapshot-only and `full_boot_verified=false`. Evidence is in
`docs/evidence/qemu-2026-10-25/amfi-stack/`.

That snapshot's PC is `0xfffffff00742b7f4`, inside
`_ml_get_speculative_timebase` (`+0x14`). The routine reads
`CNTVCT_EL0` and retries only if `cpu_base_timebase` changes during the read;
it is not itself a wait routine. Its LR, `0xfffffff007428cac`, is inside
`_ml_interrupt_masked_debug_timestamp`; source shows that helper obtains a
scheduler-hygiene timestamp while masking interrupts. The saved frame chain
also returns through `_ml_set_interrupts_enabled_with_debug`. This is evidence
of the sampled execution context in this QEMU snapshot, not evidence of an
IOKit wait or proof that the physical phone stopped in the same code. The
old 600-second `qlong` result exists only as a prose note: its raw registers,
stack, and exact input payload were not found, so its PC cannot be upgraded
to a backtrace or continuous-progress measurement.

I also tried `startup_debug=1` and `startup_debug=0x1` with this QEMU payload.
The source defines this tunable as `STARTUP_DEBUG_VERBOSE`, but neither
snapshot displayed callback/phase messages beyond the existing console. Its
argument parsing or output path needs confirmation before relying on it. The
next instrumentation should add explicit, low-volume markers immediately
after the EARLY_BOOT dispatcher, after screen/commpage initialization, after
LOCKDOWN, and at entry to `bsd_init()`. In parallel, add periodic QEMU-only
QMP register/stack snapshots or a working GDB transport to distinguish a
moving PC from a repeated wait/loop. Run those changes only in QEMU first.

No root filesystem was attached, no root device was mounted, and no intended
userspace was launched. Neither console text nor an empty panic scan proves
a complete kernel boot.

### Explicit startup trace patch

Patch `xnu/patches/0087-a30s-startup-progress.patch` adds one-shot
`SWEETTRACE: A30S_*` `printf` markers after EARLY_BOOT dispatch and `spllo()`,
after screen/commpage setup, after lockdown callbacks, after
`machine_lockdown()`, and at `bsd_init()` entry. These use the framebuffer
`printf` path that displayed the AMFI line. The patch changes no scheduler,
interrupt, timer, or memory-management logic.

The Actions kernel for PR merge `bce17d818a4b748e86fff776e6fa7f4c7f1bbdb4`
contains all five markers; its kernel SHA-256 is
`75ad1fbf9b105b2474e9eb67cedc1adb306e6cffd317ee33bb40112e19d6171d`. The
first built revision placed `A30S_EARLY_BOOT_DONE` before `spllo()`. Both
direct-entry and shim/SBOOT-state QEMU runs panicked at
`Console I/O from interrupt-disabled context @serial_console.c:168` when that
marker called `printf`. XNU's guard rejects console I/O once startup phase is
EARLY_BOOT while interrupts remain disabled. This locates the failure after
the EARLY_BOOT dispatcher returned but before `spllo()` completed; it is an
instrumentation placement bug, not a demonstrated hardware or IOKit failure.

Repeated QMP samples confirmed the simulated panic state persisted: direct
entry recorded 11 samples over 60 seconds, and shim/SBOOT-state recorded 7
over 40 seconds. Each stayed at PC `0xfffffff0072a2bb4` with an unchanged
stack pointer for that run. QEMU ended paused for the snapshot and the host
harness then killed it. No trace marker was observed and
`full_boot_verified=false`. Evidence is in the separate candidate's
`qemu/direct/` and `qemu/shim-sboot-state/` directories. The candidate's
outer and payload command lines omit `emmcprobe`; **do not use this failed
candidate on hardware**.

The working-tree patch now moves `A30S_EARLY_BOOT_DONE` to immediately after
`spllo()`, still after EARLY_BOOT dispatch. That correction has only passed a
patch-application check; it has not been rebuilt or tested in QEMU. The next
required step is a new Actions build from the corrected patch, followed by
QEMU snapshots to check the trace sequence before any hardware decision.

A separate earlier QEMU session recorded a different CI kernel
(`8d181ef91cb070cd0c5a5bf3d4bd4603df9c4c91915530a2859864c0ce8edd9d`) and
Actions merge `96aad960f7d15992ba366a4be597524f4c31de17`, associated with
branch commit `ee6552674b8c29a0195165a23c4ee87b5f7b4bed`. That is historical
evidence and is not the trace kernel analyzed above (SHA-256
`75ad1fbf9b105b2474e9eb67cedc1adb306e6cffd317ee33bb40112e19d6171d`) or the
current branch tip (`18b4775625a6e92df258e4faf8f82d5446f5ec55`). Each QEMU
payload omitted `emmcprobe=1`; QEMU does not model the Exynos host or phone
eMMC.

The user subsequently reported booting the 4 KiB candidate on the physical
A30s and seeing `SWEET AMFI: built-in interface registered`, followed by no
new console output. The exact image on the phone and its register state were
not independently read back. This is evidence of progress to the reported
console line only; it does not establish that later startup completed,
hardware initialized, eMMC was read, a root filesystem mounted, or iOS
userspace ran. The QEMU runs do not emulate the Exynos 7904 or validate
physical eMMC access.

## Read-only physical log collection

No fresh XNU boot log can currently be collected without first running an
XNU image on the phone. While the phone stays in its currently booted Android
system, `adb shell dmesg` and `adb logcat -d` would be read-only queries, but
would report Android's kernel/userspace logs, not the experimental XNU run.
This session could not connect ADB: its daemon failed because the sandbox has
no `/dev/bus/usb` and disallows the netlink/socket setup.

`tools/sweet-log-pull.sh` is not a safe substitute at this stage. It requests
a recovery ADB shell, resolves userdata, then raw-reads its final 1025 4 KiB
blocks and assumes a `SWEETLOG` header. The present tree does not demonstrate
that the log writer reserves and flushes that exact area; the script also
documents booting OrangeFox, which would require a reboot. Do not run it or
reboot for this test. The existing phone logs in `NOTES.md` are historical
user observations, not newly retrieved device output.

Next validation choices: first expose the still-running Android device over
ADB for read-only identification and Android logs (this will not produce a
fresh XNU log), or separately authorize a controlled physical test of the
4 KiB kernel after reviewing its exact image/provenance and recovery path.
No eMMC probe or hardware-path change is part of either choice here.
