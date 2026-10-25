#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the A30s xnu needs to boot.

Port of mkadt.py (ios-sweet) to the Galaxy A30s (Exynos 7904). Only what
the early boot path actually reads; every MMIO address comes from the
A30s live FDT (P1 recon, NOTES.md). Usage:

  mkadt-a30s.py OUT.adt [--dump] [--qemu]

--qemu switches the boot-core reg to 0 for the QEMU virt board: there
the single TCG vCPU reports Aff1=0/Aff0=0 (mask MPIDR_AFF1|MPIDR_AFF0
in ml_get_cpu_number), while the phone boot core is P1-measured
MPIDR=0x80000100 (Aff1=1, Aff0=0). A mismatch trips asserts in
machine_routines.c on BOTH sides.

Data model differences vs the Qualcomm reference:
  - single CPU node (P2.0 is single-core; patch 0082 has no SMP IPIs
    beyond the self-SGI), boot core physical id 0x100 (A73 big cluster,
    P1 sec_probe-measured MPIDR 0x80000100);
  - GIC-400: /arm-io/gic reg = {GICC 0x12302000+4K, GICD 0x12301000+4K},
    SOC-relative like every /arm-io child (patch 0082 reads exactly
    this layout and optional virtual-timer-intid);
  - virtual timer: FDT `timer` arm,armv8-timer says PPI 11 -> INTID 27,
    CNTFRQ 26 MHz;
  - framebuffer 0xEC000000 720x1560 stride 2880 (SBOOT leaves it up);
  - kernel RAM home = bank2 0xC0000000-0xF5400000 (cleanest band: no
    SBOOT/TZ carve-outs like bank1) - dram-base/dram-size report
    0xC4000000 + 0x31300000 after carving the 1 MiB panic log at
    0xF5300000..0xF5400000.
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: bank2 usable band; see the module docstring.
PHYS_BASE = 0xc4000000
MEM_SIZE = 0x31300000		# 0xF5400000 (next reserved) - PHYS_BASE - PANIC_LOG_SIZE
# Panic log buffer, carved off the TOP of the dram range and exposed to
# XNU as /pram + /chosen/embedded-panic-log-size. Without it the kernel
# hangs right after "kdp_core zlib memory" (reproduced upstream on QEMU
# and independently on ios-fajita).
PANIC_LOG_SIZE = 0x100000
# /arm-io children are relative to this; it must not be 0 (serial_init
# bails). Exynos peripherals start at 0x10000000 (wdt/PMC/GIC/serial).
SOC_BASE = 0x01000000

# GIC-400 (fdt interrupt-controller node: 4 regions GICD/GICC/GICH/GICV)
GICC, GICC_SIZE = 0x12302000, 0x1000
GICD, GICD_SIZE = 0x12301000, 0x1000
# --qemu re-homes the GIC-400 on the QEMU virt board map, where the
# emulated GIC-400 lives: GICD 0x08000000 / GICC 0x08010000, so the
# kernel's v2 pe_init_fiq programs the real controller (run the VM with
# -M virt,gic-version=2).
GICC_QEMU, GICD_QEMU = 0x08010000, 0x08000000
# fbcon: s3cfb.bootloaderfb=0xEC000000 (also a stock reserved-memory node)
FB, FB_SIZE = 0xec000000, 0x1400000
FB_W, FB_H, FB_STRIDE = 720, 1560, 720 * 4
# Synthetic ECID, NOT the phone's: 'A30S' header word, then a fixed tail.
A30S_ECID = 0x4133305300000001
TIMEBASE = 26000000		# CNTFRQ at boot (P1.3), FDT clock-frequency 0x18cba80
VTIMER_INTID = 27		# FDT timer: type1 PPI 11 (virtual) -> INTID 27
EMMC_PA, EMMC_SIZE = 0x13500000, 0x2000  # Exynos 7904 dwmmc0, eMMC host
# cpus=1 for P2.0: boot core only (P1 sec_probe measured the boot MPIDR
# as 0x80000100 = Aff1 1, Aff0 0 - the big Cortex-A73 cluster). Single
# CPU = patch 0082 GICv2 self-SGI scope.
#
# --qemu replaces it with 0: QEMU's virt vCPU has all affinities 0, and
# ml_get_cpu_number() masks MPIDR_EL1 to AFF1|AFF0=0xffff and compares
# against the raw ADT reg (assert at machine_routines.c:1244).
BOOT_CORE_PHYS_ID = 0x100
BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW; /product
# comes from the real D22AP tree when available (kept out of git):
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/.../DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
			 'research', 'd22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node
    bookkeeping."""
    if not os.path.exists(path):
        print(f'note: {path} missing, no /product node (fine for first boot)',
              file=sys.stderr)
        return None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import adt as _adt
    for c in _adt.parse(open(path, 'rb').read())[0].children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props
                    if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')


u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def build():
    qemu = '--qemu' in sys.argv
    # Carve the panic log off the top so the kernel never allocates
    # over it; /pram sits in the 1 MiB hole, below the 0xF5400000
    # reserved band.
    panic_base = PHYS_BASE + MEM_SIZE
    gicc, gicd = (GICC_QEMU, GICD_QEMU) if qemu else (GICC, GICD)
    boot_core_reg = 0 if qemu else BOOT_CORE_PHYS_ID
    gicc_rel, gicd_rel = gicc - SOC_BASE, gicd - SOC_BASE

    seed = os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(PHYS_BASE)),
        ('dram-size', u64(MEM_SIZE)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('a30s-shim')),
        ('system-firmware-version', cstr('a30s-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits
        # unless it exists (userland step, harmless for first boot).
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm"
        # without it; real A11+ trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt screen class (720x1560 @2x = 360x780 points).
        ('display-scale', u32(2)),
        # XNU derives IOPlatformUUID from these; synthetic ECID (not
        # the phone's); chip-id of the A11 the iPhone X userland wants.
        ('unique-chip-id', struct.pack('<Q', A30S_ECID)),
        ('chip-id', u32(0x8015)),
        # Dev-fused, undemoted security tuple 0,0,1,1 (MobileGestalt +
        # mobileactivationd DeviceType native "Hactivation" branch).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [
        node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like
        # `csrutil disable` (lp-sip0), and lp-sip1: unauthenticated
        # (unsealed) root — the iOS shared cache maps only with
        # CSR_ALLOW_UNRESTRICTED_FS.
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))]),
    ])

    # P2.0: exactly one CPU - the boot core (fdt cpus/cpu@100).
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], [
        node('cpu0', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('arm,cortex-a73') + cstr('ARM,v8')),
            ('reg', u32(boot_core_reg)),	# MPIDR Aff1:Aff0, not logical
            ('cpu-id', u32(0)),
            ('cluster-id', u32(0)),
            ('cluster-type', u32(0)),		# homogeneous SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(0)),
            ('l2-cache-id', u32(0)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running')),
            ('timebase-frequency', u32(TIMEBASE)),
            ('clock-frequency', u32(1350000000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1600000000)),
            ('peripheral-frequency', u32(TIMEBASE)),
            ('fixed-frequency', u32(TIMEBASE)),
        ]),
    ])

    # /arm-io/gic: EXACT layout patch 0082 expects - reg = {gicc base,
    # gicc size, gicd base, gicd size}, SOC-relative. virtual-timer-intid
    # overrides sweet_vtimer_intid at pe_init_fiq() time.
    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v2')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(VTIMER_INTID)),
        ('reg', u64(gicc_rel, GICC_SIZE, gicd_rel, GICD_SIZE)),
    ])

    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('a30s-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [
        gic,
        # No hardware driver: watchdogd checks IODeviceTree:/arm-io/wdt
        # for no-pmu and otherwise crashes setting the PMU boot stage.
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer counts /arm-io children
        # with this device_type and reads the canvas; AppleCLCD matches
        # the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(FB_W)),
            ('canvas-height', u32(FB_H)),
        ]),
    ])

    # Kernel framebuffer console (patch 0003): absolute physical reg.
    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(FB, FB_SIZE)),
        ('width', u32(FB_W)),
        ('height', u32(FB_H)),
        ('stride', u32(FB_STRIDE)),
    ])

    # pmap_compute_max_asids requires the node; no effaceable storage:
    # keybagd then reads the systembag as a plain plist.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons
    # home-button-type (D22AP: 2 = no Home button; keep while running
    # the iPhone X userland identity).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    # Panic log region: /pram reg = <phys size>; sits just above the
    # dram range we expose so XNU maps it but never allocates into it.
    pram = node('pram', [ph(),
                         ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # Samsung universal7904 board DTS: dwmmc0 at 0x13500000 is the
    # non-removable 8-bit eMMC host. The first driver stage only reads its
    # registers when explicitly requested with emmcprobe=1.
    emmc = node('sweet-emmc', [
        ph(),
        ('compatible', cstr('samsung,exynos-dw-mshc')),
        ('reg', u64(EMMC_PA, EMMC_SIZE)),
        ('bus-width', u32(8)),
        ('non-removable', b''),
    ])

    # Identity of the iPhone X whose userland runs here.
    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props \
        else []

    # compatible[0] = hw.model/hw.target (D22AP), model = hw.machine
    # (iPhone10,3); 'xiaomi,sweet' stays for SweetPlatformExpert
    # matching; 'exynos7904' describes the SoC honestly.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet')
         + cstr('exynos7904') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram, emmc] + product)


if __name__ == '__main__':
    root = build()
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
