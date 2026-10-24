# ios-a30s — notes / active handoff

NOTES-style handoff for the Samsung A30s XNU bring-up project. Formal
sibling of `../ios-sweet` (read-only upstream reference; never commit
device dumps or Apple artifacts into either repo).

## Goal

Boot Apple's iOS 16.7.16 userland on the Galaxy A30s using upstream
XNU (same recipe as ios-sweet, different SoC: Exynos 7904).

## Status

- **Phase 0 (recon) — done.** Full facts in the session report; key
  pointers: GIC-400 @ 0x12301000 (GICv2, not the GICv3 of sweet), DT
  declares PSCI (cpu_suspend/cpu_off/cpu_on, method=smc: Samsung's EL3
  firmware ID 0xc4000003), RAM is LPDDR4 4 GiB in three banks with a
  clean upper bank 0x880000000-0x8FFFFFFFF, eMMC (dw_mmc hosts), panel
  S6E8FC1 via DECON-F/DSIM with a framebuffer SBOOT exposes on the
  cmdline as `s3cfb.bootloaderfb=0xEC000000`, touch = Imagis IM0115
  (I²C 0x50), power key and battery via S2MU106 PMIC, no UART without a
  jig, boot = Samsung SBOOT + Download Mode (no fastboot).
- **Phase 1 (probe shim) — code built, NOT yet flashed.**
  `shim/a30s.c` paints eight result bands on the SBOOT splash
  framebuffer and answers: is x0 a valid FDT (/cpus parseable), does
  the PSCI SMC layer respond, is the upper RAM bank cacheless R/W-clean,
  is the GICD sane (read-only), does the watchdog go off, does PSCI
  CPU_ON wake secondaries whose own MPIDR they report back. No storage,
  no MMU work, no payload. Everything is communicated on screen since
  there is no UART.

## Infrastructure

- Toolchain: host clang (`clang --target=aarch64-unknown-none-elf`) +
  ld.lld + llvm-objcopy + host `mkbootimg`. No sudo needed, nothing
  installed system-wide.
- Build: `shim/build.sh` → `shim/out/shim-a30s-p1.img` (Samsung-style
  boot header v1, page 2048, kernel at 0x10008000, kernel size
  0x14000 = 82 KB).
- The shim image carries an entry branch at BOTH file offset 0 and the
  ARM64-spec offset 0x80, so either SBOOT entry convention works.

## Restore plan for the flash steps (to be confirmed with the user)

- `../backup_samsung/` holds MD5-verified backups of boot, recovery,
  param, efs, radio. NOTE: `backup_samsung/boot.img` is the CURRENT
  Magisk-patched boot (md5 = the live one), not day-one stock.
- Never touch `recovery`/`param`/`efs`/`radio` — recovery stays intact
  so a failed boot partition can be restored from the SD card with adb
  in OrangeFox (they keep their own kernel).
- Flash path: `adb push` shim image + current-boot backup to the phone
  SD, then `su -c dd if=/data/local/tmp/… of=/dev/block/mmcblk0p15`.
  Confirmed with the user BEFORE running.

## P1 boot report (2026-10-23 17:12, user read the screen)

- **SBOOT passes a valid FDT in x0** (0x8A000000, magic d00dfeed,
  totalsize 0x29421) and enters at **EL1** (el=0x00000004), boot CPU
  mpidr 0x80000100. Entry-point convention (0 and 0x80) fine.
- **RAM/GICD/WDT probes green**: upper bank clean cacheless R/W,
  GICD_CTLR=0 GICD_TYPER=0xFCEF → 16 GICD blocks → 512 INTIDs, WDT
  writable. **CNTFRQ = 0x018CBA80 = 26 MHz** (recon assumption held).
- **Bands 0/5/6 red = shim bug, not firmware**: the FDT walker counted
  the root node as depth 1, so `/cpus` (real depth 1) never matched the
  "`/cpus` name at depth==1" check → 0 CPUs parsed → CPU_ON never ran
  → bands 5/6 red as a consequence.
- **PSCI semantics red = expectation bug**: PSCI_VERSION and SMCCC
  return 0xFFFFFFFF (firmware implements no 0.2 calls) — consistent
  with the legacy `arm,psci` trio-only node. CPU_ON itself is *not*
  disproven; verdict pending P1.1.
- Fixed in P1.1 (commit after this note): root-node special-casing in
  the walker (root = depth 0), per-CPU aff printout, band 1 turns
  AMBER for a trio-only firmware (GREEN-band semantics unchanged), and
  band 7 summary treats AMBER as pass for informational bands.

## P1.2 boot report (2026-10-23 ~17:30)

- Same band pattern as P1.1: 0 green, 1 amber, 2-4 green, 5 green
  (CPU_ON accepted, on=7), 6 RED (no acks), 7 dark (summary).
- SECACK zeros for slots 1..5 (aff 0x101..0x201). **The rest of the
  text (SLOT0, CORES, remaining pairs) CLIPPED off-screen** — P1.2
  added 7 SECST lines, overflowing the 1560 px text budget (console
  clips silently). Screen-cut reported by user ("thats all i see").
- Diagnosis upgraded to competing hypotheses (all still live):
  (a) firmware delivers x0=0 to every CPU_ON secondary, racing them
      into slot 0 (not read, clipped);
  (b) legacy PSCI 0.1 argument order (x1=entry, x2=cpu id): the 0.2-
      order calls pointed cores at the cpu aff value (0x101 etc.) as
      the "entry" — junk PC, instant abort, no observable;
  (c) firmware accepts CPU_ON but never re-vectors (seconds stay in
      the firmware park);
  (d) secondaries enter outside EL1 in a state where the P1.2 record
      never lands.
- Also found a latent **unguarded-slot bug** in the P1.2 stub: the
  x0-slot record store had no bounds check — a garbage x0 would have
  corrupted memory past sec_probe. Fixed in P1.3 with a x0<=15 guard.

## P1.3 boot report (2026-10-23 ~17:35) — FIRMWARE FREEZE

- Screen froze: bands 0-4 lit (0 green, 1 amber, 2-4 green), text ends
  at FRQ, rest of the frame static gray, other bands dark. No CPUA
  line → freeze inside the FIRST CPU_ON smc.
- Cause found in the sources: the variant letters were offset by one
  (mode = 'a'+i with i the RAW cpu index, but i=1 is the FIRST
  non-self core) → the very first call was the swapped legacy-0.1
  order (x1=entry, x2=cpu) instead of the standard order.
- **Finding: the swapped-argument CPU_ON call hard-freezes the
  Samsung EL3 firmware.** The same core (0x101) with the standard
  order returned cleanly in P1.1/P1.2 — so (x1=cpu, x2=entry) is the
  accepted shape and (x1=entry, x2=cpu) is not merely wrong, it kills
  the firmware handler. Treat swapped calls as banned on this device.
- Phone power-cycled through the proven Recovery break-in, no harm -
  boot restore never needed.
- P1.3.2 (commit 1a2ab75): mode bug fixed; standard-order only;
  PRE/POST progress bar (16x8 cells at y=294, x=24+20*(i-1)) painted
  before/after every CPU_ON — a frozen BLUE cell marks the smc that
  hung; POST colors: WHITE=ret 0, RED=non-zero.located FB tick +
  sec_probe channels unchanged — the decisive "did a secondary run"
  instrument is now paired with a safe call sequence.

## P1.3.2 boot report (2026-10-23 ~17:40) — FINAL CPU_ON VERDICT

- Standard-order CPU_ON for all 7 secondaries: every call returned 0
  (progress bar all white cells), no freezes. But: no MP* records, no
  CS* records, TICKS bits=00000000 — all three observation channels
  (Device-memory fb tick / mpidr-slot cells / guarded x0-slot cells)
  silent. Band 6 RED, summary band 7 RED. No white dots.
- **Verdict: PSCI CPU_ON on this firmware returns success but never
  boots the secondary — a cosmetic no-op.** fb tick is el/x0/cache-
  proof, so nothing reached the stub. P1.2's "on=7" was never real
  "on".
- P1.4 relationship: PMC delta-0 (below) closes the loop — the
  firmware handler never even powered a core.

## P1.4 boot report (2026-10-23 ~17:5x) — PMC VERDICT (read-only)

- PMU snapshot at shim boot (cfg/st at stock cal-if offsets):
  - boot core 0x2200 = CLUSTER1_CPU0 (mpidr 0x100): cfg 0x8000000F,
    st 0x104C000F → powered.
  - all 7 secondaries (0x2000, 0x2080, 0x2280, 0x2300, 0x2380,
    0x2100, 0x2180): cfg 0x80000000, st 0x104C0000 → powered OFF.
  - low nibble mirrors on/off; bit31 set on powered cores.
- Delta after all CPU_ONs: zero. **The TC handler never wrote a PMC
  bit.** TICKS zero, no records.
- Stock tree reconciles: DT enable-method=psci for all 8 cpus,
  drivers/firmware/psci.c standard psci_cpu_on; AND cpuinfo.txt shows
  8 processors running daily — so stock boots 8 cores with the SAME
  FID that no-ops from our shim. Unexplained gap (TC context gating?
  register-validation?). Not pursued further: manual PMC path is
  next and needs no firmware cooperation.
- cal_cpu_enable/cal_cpu_disable/cal_cpu_status exist in stock
  (cal-if.h, backed by pmucal_cpu.c) — the kernel-side CPU power API;
  callers not identified in the fetched subset (cpuidle-exynos64.c
  clean, el3_mon.c is only kernel-protection/debug-mem registration).
- pmucal_cpu_exynos7885.h register map fully extracted:
  - per-core on = CONFIGURATION |= 0xF; off = &= ~0xF; status mask 0xF.
  - CLUSTER1 per-core on needs NO preamble (single write).
  - CLUSTER0 per-core on needs: SHORTSTOP @0x10900000+0x820 := \
    0xFFFFFFFF, then CLUSTER0_CPUSEQUENCER_OPTION @0x2488 bit4|=1 and
    bit0|=0, then cfg |= 0xF.
  - CLUSTER2 per-core on: same shape with SHORTSTOP @0x10A00000+0x820
    and CPUSEQUENCER_OPTION @0x24C8.
  - whole-cluster on/off = CPUSEQUENCER OPTION writes (0x2488 /
    0x24A8 / 0x24C8), status @0x2484/0x24A4/0x24C4 mask 0x1.
  - SMC_CMD_CPU1BOOT (-4) is defined but unused in this tree —
    legacy; CPU1BOOT experiments are NOT in P1.5.

## P1.5 design (2026-10-23, built next) — MANUAL PMC POWER-ON

- SMC-free probe (no new firmware calls, no freeze risk):
  1. full-page PMU snapshot (0x11c80000..0x11c8ffff, 16K words, BSS).
  2. cluster1 CPU1 (mpidr 0x101): cfg @0x2280 |= 0xF (preset progress
     bar BLUE cell at y=294 x=24, overpaint white after).
  3. poll st @0x2284 low nibble up to ~1.5 s; print SC1P line (cfg,
     st at poll end) + seconds-to-flip if it came up.
  4. 1.5 s observation window: fb tick (slot 5), sec_probe (MP5),
     guarded x0-slot cells, then full-page PMU diff (print only
     changed offsets, cap 13 rows + PMDIFF n= count).
- Bands: 5 = PMC write accepted (status flipped), 6 = any secondary
  channel (tick/records), 7 = summary. No CPU_ON calls at all.
- Expected outcomes: (a) status flips + tick/records → secondary
  boots into OUR stub (park pen open!) → P2 SMP; (b) status flips but
  no channels → core up but parked somewhere unknown → P1.6 hunts
  the park/pen (sysram diff, CPU1BOOT probe as fallback); (c) status
  stays 0 → PMC write insufficient (needs more context) → stock
  driver deep-dive before retry.

- FB tick: every core that reaches secondary_entry paints a 4x4 white
  rect at y=1496, x=8+slot*8 (slot = aff0+4*aff1, from MPIDR, never
  from x0). Base byte offset in fb: (1496*720+8)*4 = 0x41BE20, row
  pitch 2880 B. Decisive, cache/el/x0/compiler-proof probe ("did the
  PC actually get here").
- sec_probe resized to 128 words: [0..63] mpidr-slot cells, [64..127]
  guarded x0-slot cells (32 B each: x0, CurrentEL, mpidr).
- Only-nonzero prints + FRQ moved before probes; ~31 lines budget.
- Per-core CPU_ON protocol tests: 0x101 standard (0x2-style),
  0x102 swapped legacy-0.1 order, 0x103 standard + delayed re-call
  (ALREADY_ON = 0xFFFFFFFC check), 0x200/0x201/0x0/0x1 standard.
- Band 6 green if ANY channel fires (mpack, cslot, or tick bits).
- Fallback path if everything stays zero: Exynos-PMU manual power-on
  (fdt exynos-pmu node @ line 5111 in fdt.dts; recon REPORT unknown #2).

## P1.5 boot reports (2026-10-23 ~18:03–18:2x) — MANUAL PMC WAKE: PARTIAL SUCCESS + NEW FAULT

### P1.5 (45 pages, e40d088, md5 99ab8e97)
- PANICKED on boot core before any instrument: SLOT idx=0 cpu=80000100,
  STG ptr=8017EE9B. Blue-clear panic screen (old handler destroyed the
  scene). Prime suspect then: full-page 64 KiB PMU scan (P1.4 had only
  ever touched 16 offsets).

### P1.5b (29 pages, 0f7c7e7, md5 85c994ca)
- PMU access trimmed back to the P1.4 16-value set. STILL panicked
  "a few ms after text showed": SLOT idx=0 cpu=80000100, STG
  ptr=8015f02e. Full-page reads eliminated as the sole cause.
- ptr decode: 0x8015f02e − 0x302e ("psci" string offset) = 0x8015C000
  page-aligned → stage ptr was probably VALID, just unknown-base.

### P1.5c (29 pages, ee3960a, md5 fc636bf1) — FORENSICS RUN
- Crash scene now preserved (no clear): red stripe top + SLOT/ESR/FAR/
  CUR/BASE lines; runtime BASE print; 15-cell step-mark trail y=1464.
- LIVE RESULT (user transcript, noise-corrected):
  bands: [red stripe over 0] amber(1) green(2,3,4) GREEN(5) red(6)
  marks: 0 white, 1–6 green, 7 blue(pre-write), 8 white(written),
         9 green(poll→FLIPPED), 10 amber(SC1P), 11–12 green;
         panic AFTER step 12, inside pm_delta.
  ESR  hi=00000000 lo=96000010   FAR hi=00000000 lo=00000000
  CUR  ptr≈8010F28A              BASE lo≈8010F8C0 (&sec_probe)
       → stage ≈ base−0x636 (decodable once; pointer VALID)
  SLOT idx=00000004 cpu=80000100 (vec slot 4 = current-EL SPx sync)
  PSCI ver=FFFFFFFF smccc=FFFFFFFF (legacy trio, unchanged)
  C2   cfg=8000000F stv=0007000F (0x2200 boot core, PRE-write!)
  PMBASE at=11C80000 seqn=00060000 (@0x2484)
  SC1P cfg=8000000F stv=0007000F pre=80000000 (core 0x101)
  TICKS bits=00000000  CORES on=1 ack=0
  D3 fs2=104C0000 nows=0007000F  (D3 pair PRINTED, then abort)

### P1.5c decode + verdict
1. **Manual PMC power-on WORKS (first ever):** core 0x101 cfg write
   accepted, status flipped to 0xF (SC1P + D3 + band5 green). The
   stock cluster1 single-write sequence from the pmucal table is
   faithful on this board.
2. **But the core never reached our channels:** TICKS 0, ack 0, no
   records → status=powered ≠ executing our stub. Park/reset target
   after PMC wake is the missing link (outcome (b) of the design).
3. **PMC status readback SHAPE changed globally:** powered-off mask
   was 0x104C0000 (P1.4); now the FLIPPED core reads st=0x0007000F
   and even the UNTOUCHED boot core 0x2204 reads st=0x0007000F
   pre-write this run (P1.4 gave 0x104C000F). PMC sequencer engaged
   cluster-wide; status semantic not understood yet. seqn @0x2484
   reads 0x00060000.
4. **New fault class:** sync EXTERNAL abort (DFSC=0x10, WnR=READ,
   FAR=0) on a PMU SFR read AFTER the wake — D3 printed, abort on the
   next register read (pmu_core_off[4]=0x2300 window 0x2300..0x2384).
   The SAME page was fully readable pre-write in the same run → the
   wake leaves the PMC in a busy/unsteady state where some SFR reads
   bus-error. NOT a mapping/pointer issue (FAR=0).
5. **SBOOT load base drifts per boot:** BASE 0x8010F8C0 this run vs
   ~0x8015C000 prior — image NOT loaded at the header's 0x10008000.
   Shim is PC-relative (zero abs relocs) so unaffected; XNU phases
   must not assume any fixed load address either.
6. ESR EC nibbles: 0x96000010 → EC=0b100101 (37, lower-EL data abort)
   as transcribed; possibly a misread of 0x96200010 (36, current-EL).
   Either way: data abort family, external abort cause is solid.

### DECISION (user, 2026-10-23): SMP PARKED
- User asked "do we have to do this to port ios?" — answered honestly:
  no; SMP is an optimization, XNU boots/needs single core; probes were
  diagnostic instruments, not gates. User chose: read the panic lines
  (done), then park SMP and start Phase 2 (single-core XNU).
- Deferred P1.6 lead list (for the eventual SMP return):
  a. post-wake access policy: read ONLY 0x2280/0x2284 after wake,
     defer all other PMU reads ≥1.5 s (is the bus wedge transient?);
  b. WDT-guarded runs (auto-reboot instead of panic-hang);
  c. target a cluster0/cluster2 core whose power-on needs the
     SHORTSTOP+CPUSEQUENCER preamble (different sequencer state);
  d. legacy SMC_CMD_CPU1BOOT (−4) probe (defined-but-unused in tree);
  e. hunt the park target: sweep DRAM/SYSRAM for CPU1 fetch address
     after wake (sec_probe/tick/records currently never fire);
  f. resolve st 0x0007000F shape: poll it over seconds from boot to
     see if it decays back to 0x104C-shape.

## Phase 2 architecture (2026-10-23, after surveying ios-sweet)

The upstream Redmi port (ios-sweet, 84-patch series) is the template.
ios-a30s now carries a verbatim copy of its infrastructure:
- `xnu/patches/` = the whole series, applied by CI onto stock
  xnu-8796 (macOS 13.5's kernel = iOS 16.7 era) via
  `.github/workflows/xnu.yml` (blacktop/darwin-xnu-build at pinned SHA,
  macOS-14 runner, MACHINE_CONFIG SWEET). Artifact = kernel.* binaries.
- `tools/` = mkpayload.py (kernel+ADT+ramdisk+cmdline blob the shim
  jumps into), mkadt.py (hand-written ADT), qemu/usb/log helpers.
- Upstream clone stays pristine as reference; divergence in ios-a30s
  happens as NEW patches appended after the existing numbering, plus
  runtime-matched ADT values.

Machine-config strategy: KEEP the name SWEET (minimizes churn; every
sweet patch is `#if defined(SWEET)`-keyed and compiles unchanged with
`ARM64_BOARD_CONFIG_SWEET`). Board identity (cpu type, product name,
IONameMatch strings) lives in the ADT we feed, not in patches:
- SweetPlatformExpert matches root compat `xiaomi,sweet` -> our ADT
  root compatible will include that trailing alias.
- SweetCPU matches `qcom,kryo470` CPU nodes -> our ADT cpu*/ reg nodes
  carry compatible strings that still list it (Cortex-A73 boot core;
  runtime identity strings for hw.product come from patch 0059 = ADT
  /product anyway, so MobileGestalt still reports iPhone identity).

### Interrupt/timer architecture learned from the upstream patches
- pexpert/arm/pe_fiq.c upstream has HAS_GIC_V3 code (reads the GIC DT
  node reg). SWEET: group0 is TrustZone's; everything (vtimer, SGIs)
  rides Group 1 NS IRQs by patch 0004/0017; sleh.c sleh_irq()
  replaces the standard IRQ flow: ICC_IAR1_EL1 -> routes
  SWEET_VTIMER_INTID to rtclock_intr, SGI (SWEET_IPI_SGI=1) to
  cpu_signal_handler, else cdp->interrupt_handler; EOI ICC_EOIR1.
- vtimer INTID is ALREADY ADT-overridable: patch 0017 reads optional
  `virtual-timer-intid` (u32 PPI INTID) from the ADT GIC node.
- SweetCPU (0018): legacy-IOCPU boot-CPU bring-up, ml_processor_register,
  SGI1 self-test via ICC_SGI1R_EL1 (GICv3 system-reg path).
- SweetCPU startCPU returns KERN_NOT_SUPPORTED = single-CPU mode is the
  design baseline. Our SMP probe returns as a LATER task.

### P2.1 DONE (2026-10-23): patch 0082-a30s-gicv2-interrupts written + verified
- Numbering discovery: the upstream series is LONGER than first surveyed —
  it really runs to `0081-sweet-usb-link-state.patch` (their new-era
  patches: 0078 = sweet-smp-8-cores, 0079 = display power, 0080 = net
  trace, 0081 = USB link state). Our first own patch is therefore
  **`0082-a30s-gicv2-interrupts.patch`** (321 lines, 5 files, +229/−2).
- Verify loop (local reconstruction): `/tmp/opencode/xnu-tree` =
  vanilla xnu-8796.141.3 (`1b191cb`), 3 `.sh` stages run
  (libpthread-514.100.2, BearSSL, hfs-627.100.6 via fake
  `/tmp/opencode/bin/xcrun` + stub TargetConditionals.h, env
  MACHINE_CONFIG=SWEET MACOS_VERSION=13.5), then `git apply` in glob
  order: with 0082 present **applied=82 failed=0**. Baseline commit in
  the recon tree: `340f918`.
- Non-conflict check vs upstream `0078-sweet-smp-8-cores.patch`: it
  only widens SweetCPU topology limit (phys_id ≤ 0x700, core text) and
  the cpus opt-in string in machine_routines.c; our hunks (signalCPU
  GICv2 branch, pe_fiq.c, sleh.c) never overlap. Both patches coexist.
- Contents of 0082:
  - `pexpert/pexpert/arm64/SWEET.h`: `#define SWEET_GICV2 1` + GICv2
    offsets (GICC_CTLR/PMR/BPR/IAR/EOIR, GICD_IGROUPR/ISENABLER/
    IPRIORITYR/SGIR + FILTER_SELF = 0b10<<8). Reuses existing
    GICD_CTLR/GICD_CTLR_ENABLEGRP1.
  - `pexpert/arm/pe_fiq.c`: inside `#else /* !HAS_GIC_V3 */`, adds
    `#if defined(SWEET_GICV2)` branch — statics gicc_base/gicd_base
    from ADT `/arm-io/gic` reg = {gicc,gicc_size,gicd,gicd_size}
    (ml_io_map'd off soc_base), `virtual-timer-intid` override read
    (PPI range-checked 16..31), helpers `pe_sweet_gic_iar()`,
    `pe_sweet_gic_eoi(iar)`, `pe_sweet_gic_self_sgi(intid)`
    (GICD_SGIR = FILTER_SELF | intid, DSB ISHST before), and a full
    `pe_init_fiq()` v2: GICC PMR=0xFF, BPR=0, CTLR=Enable; GICD
    IGROUPR×16 = 0xFFFFFFFF (all Group 1 NS), IPRIORITYR×128 bytes =
    0xA0, ISENABLER0 = (1<<vtimer_intid)|(1<<SWEET_IPI_SGI),
    GICD_CTLR |= EnableGrp1, ISB. Old stub section demoted to
    `#else /* non-GICv2 */`.
  - `pexpert/pexpert/arm/protos.h`: externs for the 3 helpers under
    `#if defined(SWEET_GICV2)`.
  - `osfmk/arm64/sleh.c`: sleh_irq gains a FIRST `#if defined
    (SWEET_GICV2)` branch: intid = `pe_sweet_gic_iar() & 0x3FF`,
    spurious check vs 1023 (`GIC_SPURIOUS_IRQ`), same 3-way routing
    (vtimer→rtclock_intr, <16→cpu_signal_handler with EOI-early,
    else cdp->interrupt_handler(intid)), EOI `pe_sweet_gic_eoi(iar)`
    — the old SWEET ICC branch demoted to `#elif defined(SWEET)`.
    `sleh_fiq()` now early-returns on SWEET_GICV2 (no ICC_IAR0_EL1
    read — that MSR would UNDEFINE/trap on GIC-400; no FIQ source
    exists, Group 0 is TrustZone's).
  - `iokit/Kernel/arm/SweetCPU.cpp`: signalCPU gets GICv2 branch →
    `pe_sweet_gic_self_sgi(SWEET_IPI_SGI)` (target==self in
    single-CPU bring-up); v3 ICC_SGI1R_EL1 path kept in #else.
    Includes pexpert/arm/protos.h.
- GICD_SGIR encoding (Linux v4.4 irq-gic.c canonical): bit
  [23:16] TargetList, [10:8] TargetListFilter, [3:0] SGIINTID.
  Filter=0b10 = self-only — sidesteps the unknown GICC interface
  numbering of the boot core (cluster1, MPIDR 0x80000100).
- NOT yet done in 0082: decrementer/timer set-decrementer stays
  vtimer-FIQ-style; ADT side (item 2) must supply `/arm-io/gic` reg +
  `virtual-timer-intid` for the branch to init.

### Work items for the first A30s kernel boot (P2 sequence)
1. **GICv2 (GIC-400) path** in pe_fiq.c/sleh.c — **DONE, patch
   0082** (see "P2.1 DONE" above for full content + verify results).
   Live-boot uncertainty left: GIC-400 security-state layout under
   SBOOT (same group1-NS pattern expected as sweet's TrustZone; P1.3
   read GICC 0x12302000 with CTLR 0, everything clean at boot).
2. **Shim payload loader** — **DONE, P2.2** (`shim/loader.c` +
   linker/build wiring). Design + verification below (see "P2.2 DONE").
3. **ADT for A30s** — **DONE, P2.3** (`tools/mkadt-a30s.py`, first
   blob `shim/out/a30s.adt` dumped + verified; see "P2.3 DONE").
4. **CI**: needs a GitHub repo for ios-a30s + `gh auth login` (user's
   account, interactive). Workflow yml is repo-agnostic as copied.
   Then: build, `gh run download`, mkpayload+ADT+payload, flash, read
   screen.

### P2.4-PRE (2026-10-23): publish-safety audit + repo restructure
- Full-history audit (user's pushback, correct): every commit scanned
  for secrets (serial/IMEI/token patterns) — ZERO hits; largest blob
  ever committed = the 1MB text patch; the only file ever deleted =
  tools/__pycache__/ pyc; NO Samsung backups/images/chromeos-style
  blobs ever tracked (backup_samsung/ is outside this repo and
  shim/out/ is gitignored).
- Licensing audit found the real problem: upstream patches carry
  **Apple copyright headers inside newly-added files** (0058 alone:
  27 headers, "Original Code and/or Modifications" language =
  APSL-2.0 material riding in the ios-sweet patch series). Even though
  the upstream repo publishes the set under MIT, mirroring
  Apple-authored source text in OUR git is not acceptable.
- Resolution - DE-MIRROR the patch series:
  - `xnu/patches/` now contains ONLY our patch (0082 as of today);
    upstream 0001-0081 (patches + stage .sh) were git rm'ed.
  - `.github/workflows/xnu.yml` gained SWEET_REPO/SWEET_SHA
    (dipa-studio/ios-sweet @ 45263bee51df4a0f55cdf9c649af886399488ddb,
    2026-10-23 = the exact upstream state our P1/P2 work was built
    against) and a fetch step that copies upstream patches + ours
    into one merged dir in glob order before the dxb build.sh step.
  - Local verify reproduced the CI path: fetch pinned SHA to
    /tmp/opencode (out of git), merge, stages, apply on pristine
    xnu-8796.141.3 (1b191cb): **applied=82 failed=0**.
  - xnu/README.md rewritten (fetch-at-build-time provenance);
    LICENSE added (MIT, kotorola, with provenance paragraph: parts
    from ios-sweet MIT; no Apple sources stored; APSL note for the
    built kernel's Apple portions; patch-context lines noted as the
    standard applied-diff practice).
- Decision rule going forward: OUR repo carries our own files and
  quotation-context patches only; upstream series + any future Apple
  material stays out of git (fetch pinned at CI time).

### P2.2 DONE (2026-10-23): shim payload loader (embedded + initrd channels)
- New `shim/loader.c` (+ loader.h), hooked FIRST in shim_main: on any
  failure it returns a string (printed as `PLFAIL ...`) and the P1
  probe screen still runs below; on success it jumps into XNU.
- Boot contract identical to upstream loader.c (verified byte layout):
  payload header struct == 0x2a8 offset for seg table; boot_args
  Revision/Version 2, Video@40, CommandLine@108, sizeof 736; segments
  copied to `vmaddr + (phys_base - virt_base)` via MMU-off aligned
  copy64; ADT then boot_args (16K-aligned, zero-padded) behind
  `kernel_end`; `topOfKernelData` = boot_args + 16K; `deviceTreeP` =
  ADT **VA**; `memSizeActual` = mem_size; jump MMU/caches off, `x0` =
  boot_args **PA**, preceded by `dsb sy; ic iallu; dsb sy; isb`.
- Video fill = console.h A30s values: v_baseAddr 0xEC000000, rowBytes
  2880, 720x1560, depth 32, `v_display` 1.
- Payload channel 1 (primary) — **embedded**: build.sh appends the
  mkpayload blob to out/Image (16-aligned) and patches the new
  linker-script info block at the binary tail
  (`shim.ld`: symbols `payload_info`/`_payload_info`, placeholder
  0xffffffff): struct {u32 magic "A30P"=0x50303341, u32 flags=1,
  u64 off, u64 size}; `off` = payload start MINUS pinfo (byte delta,
  identical at runtime because objcopy maps vaddr 1:1 from 0). Loader
  computes `payload = (uintptr_t)&payload_info + off` — fully
  PC-relative, immune to the SBOOT drifting load base.
- Payload channel 2 (fallback) — **FDT initrd** (upstream path):
  reads `/chosen` `linux,initrd-start/end`, tolerant of 1-cell/2-cell
  encodings; arbitrated strictly by the SWEETXNU magic check.
- Rationale from the recon FDT (2026-10-23): SBOOT /chosen DOES carry
  linux,initrd-start/end (stock = 0x89000000..0x8906852c) BUT during
  P1 our boot image already had a tiny empty cpio ramdisk and the FDT
  still pointed at 0x89000000/427 KB → SBOOT's initrd pointers are NOT
  proven to track the boot-image ramdisk; the embedded channel removes
  that dependency. Channel 2 stays for upstream parity + diagnostics
  (it prints what SBOOT gives us).
- `shim/build.sh`: `./build.sh PAYLOAD [OUT]` → build loader.o with
  the same flags incl. the ABS64-relocation-zero check, link, objcopy,
  truncate to _end, patch step (python, inline), mkbootimg wrap
  (header v1, kernel = Image+payload, tiny cpio ramdisk) as
  `out/shim-a30s-p2.img` (default) — and `./build.sh` without args
  keeps building the P1 probe image with the block unpatched
  (loader inert: p1 screen as before).
- Local verification (fake SWEETXNU payload, real build chain):
  unique A30P marker, delta math, header parse, overlap guard
  (embedded payload in bank1 vs kernel at bank2 0xC4000000 → passes),
  copy64 8-alignment invariants, in-payload ADT reachable at
  adt_off, boot_args 16K alignment + top < phys+mem, entry PA =
  entry + pa_of_va_base — ALL PASS; probe Image has no A30P magic.
- Recon FDT facts banked for P2.3 (ADT):
  - GIC node reg = 0x12301000 (GICD) / 0x12302000 (GICC) + 0x12304000
    (GICH) / 0x12306000 (GICV), 4K/4K/8K/8K sizes;
  - reserved-memory (stock): fb 0xEC000000+0x1400000, 0xF5400000+
    0x7800000, 0xFCC00000+0x2000000, 0xFFC00000+0x400000,
    0xB8000000+0x200000, 0x80000000+0x1000, 0xC0000000+0x80000,
    0xE9000000+0x400000, 0xE9400000+0x400000 → best kernel home =
    bank2 0xC0000000–0xF5400000 (only the 512K slice at
    0xC0000000 reserved) — dram-base candidate 0xC4000000
    (32MB-aligned) with memSize to 0xF5400000 (=0x31400000);
  - cmdline facts: ess_setup=0x91200000, tima_log=0x200000@0xB8000000
    (bank1 near top), kaslr_region=0x1000@0x80001000 — bank1 is
    SBOOT/TZ-tracked, bank2 top is the clean zone.

### P2.3 DONE (2026-10-23): A30s ADT builder (tools/mkadt-a30s.py)
- FDT-derived A30s facts now FINAL (supersedes old P1 guesses):
  - `virtual-timer-intid` = **27** (recon FDT `timer` node,
    `arm,armv8-timer`, interrupts = PPI 13 secure-phys / 14 phys /
    **11 virtual** / 10 hyp, all `0xff01`; PPI n -> INTID 16+n).
  - CNTFRQ 26 MHz re-confirmed (`clock-frequency` 0x18cba80 =
    26000000, matches the P1 booted CNTFRQ read).
  - **Boot core correction**: recon FDT `cpus/cpu@100` is an A53
    (LITTLE) core, NOT the A73 as the earlier P1 summary said. Samsung
    layout: cpu@0..0x1 = 2x A73 (cpu6/cpu7), cpu@100..103 + 0x200..201
    = 6x A53 (cpu0..cpu5); SBOOT boots cpu@100 = logical cpu0. Boot
    core phys id 0x100 comfortably passes upstream 0078's SweetCPU
    limits (<= 0x700, (phys & 0xff) == 0).
- `tools/mkadt-a30s.py` = full mkadt port + A30s values; emits
  `shim/out/a30s.adt` (4348 bytes, dump-verified):
  - chosen: dram-base 0xC4000000, dram-size 0x31300000 (bank2 usable
    band 0xC0000000-0xF5400000 minus the 512K slice at the bank top,
    plus the 1 MiB panic-log carve at 0xF5300000..0xF5400000 -
    /pram 0xF5300000+0x100000; dram-base is 4 MiB above the bank start
    for reserve-margin; same semantics as upstream's Qualcomm
    0xb0000000 pick), random-seed u8[64] fresh per build,
    firmware 'a30s-shim', display-scale 2 (720x1560 @2x), chip-id
    0x8015 (A11 = iPhone X userland), synthetic ECID 'A30S'+1,
    security tuple 0/0/1/1, boot-manifest-hash + crypto-hash-method
    sha2-384, /asmb lp-sip0=0x6f lp-sip1=1 (relaxed SIP, unsealed root).
  - cpus: ONE node cpu0 reg 0x100 (boot core only, P2.0 single-CPU),
    timebase 26000000, cortex-a53 compat.
  - arm-io: SOC_BASE 0x01000000, ranges {0, SOC_BASE, 0xFF000000};
    /arm-io/gic reg = {0x11302000, 0x1000, 0x11301000, 0x1000}
    (SOC-relative GICC first, then GICD - exact order patch 0082
    reads), 'arm,gic-v2', interrupt-controller, virtual-timer-intid
    u32 27; /arm-io/wdt no-pmu=1; /arm-io/disp0 sweet,display canvas
    720x1560.
  - sweet-fbcon at ROOT level: reg 0xEC000000+0x1400000 (absolute,
    NOT SOC-relative), width 720 height 1560 stride 2880.
  - defaults no-effaceable-storage=1; buttons (D22AP-style, home
    button_type 2); /product absent for now (needs research/d22ap.adt
    from the iPhone X IPSW; first boot does not need it).
- Consumes our upstream artifacts unchanged: mkpayload.py reads
  dram-base/dram-size via adt_dram() and injects optional --ramdisk
  into /chosen/memory-map; SweetPlatformExpert matches 'xiaomi,sweet'
  at the end of root compatible; SweetCPU matches DT reg 0x100.
- Open items for later P2 stages (not blockers): /product node from
  the real D22AP tree (userland step), UFS node absent on purpose
  (Exynos ufshc bring-up is a later task; first boot needs no root).

### Boot-args for P2.0 (upstream recipe, trimmed)
`debug=0xc serial=3 keepsyms=1 cpus=1 -enable_kprintf_spam -noprogress
rd=md0 ignition_level=1 wdt=-1` style; console = sweetfbcon on fb.

## P2 payload image, first attempted flash (2026-10-24)

- CI run 3 GREEN (run 40102410525, commit 4fc5c64; remote squash trail
  ae046c5 -> 1bb5315 -> 4fc5c64): full xnu-8796.141.3 DEVELOPMENT.SWEET
  kernel built and linked with our 0082 GICv2 layer after two rounds
  (run1: pexpert/arm/protos.h kern_return_t in IOKit C++ TU; run2:
  HAS_GIC_V3 still 1 so pe_fiq compiled the GICv3 arm and the three
  pe_sweet_gic helpers did not exist at link; fix2 flips HAS_GIC_V3=0
  under SWEET_GICV2 and hoists sweet_vtimer_intid out of the dead arm).
- kernel.development.sweet sha256 09370a5876cd663d2c77deedd7f257a71e340
  200a413015abd1ab0d0d77754bb (17,221,688 bytes, pie arm64 Mach-O).
- mkpayload run: 16,604,944-byte payload (34 segs; header 0x2a8 seg table
  + cmdline "debug=0xc serial=3 keepsyms=1 cpus=1"); ADT regenerated
  fresh (random-seed differs per build, by design).
- Boot image: shim/out/shim-a30s-p2.img, 16,728,064 bytes, sha256
  362e1be90d6217b0d76a276bf4ceeb3c452b388951431b3d352c6c183530ece3;
  Android boot header v1 kernel@0x10008000 2048B pages, kernel_size
  0xff2f30; A30P\x01 info at 0x1d000 with PC-relative delta 0x20 ->
  payload 0x1d020 (build.sh patched, loads from drifting SBOOT base).
- Budget: 16.7 MB image vs 37,748,736-byte boot partition (~21 MB clear).
- User says boot partition untouched since the live dump used for
  backup_samsung/boot.img (md5 7e5c1f459204da19c12d81a267b3e1cd).
  Flash protocol per consensus: optional read-only on-device boot hash
  check -> adb push image + on-device hash verify -> dd -> readback
  hash == image sha -> reboot, watch screen -> end of session dd the
  backup back and reboot to Android.

## Next up (after the 2026-10-24 flash)

1. Live verification of the GICv2 path (0082): vtimer PPI 27 via
   sleh_gicv2 branch, SGI self-signaling from SweetCPU::signalCPU.
2. DW-MMC driver for eMMC (the biggest new driver; reference: Samsung
   GPL dw_mmc-exynos.c), then the iOS root in userdata.
3. DECON/DSIM niceties (panel persistence assumptions get their real
   answer live with the P2 boot itself).
