/*
 * a30s P1.5 probe — first MANUAL Exynos-PMC secondary power-on.
 * Derived from ios-sweet/shim/shim.c (MIT); follows the recon, the
 * P1.1..P1.4 live results and the stock exynos7885 pmucal register
 * map (see NOTES.md).
 *
 * Facts (P1.3.2+P1.4, definitive):
 *   SBOOT enters EL1 with a valid FDT in x0 (0x8A000000); boot CPU
 *   mpidr 0x80000100; CNTFRQ 26 MHz; GIC-400 512 INTIDs; WDT ok.
 *   PSCI CPU_ON returns 0 for all secondaries but is a TOTAL no-op:
 *   no channel ever fires (fb tick / mpidr cell / x0 cell all zero)
 *   and the PMU delta is zero — the TC handler never touches PMC.
 *   At shim boot every secondary is PMC-powered-off (cfg 0x80000000,
 *   st 0x104C0000); only the boot core (cfg reg 0x2200 = CLUSTER1_
 *   CPU0) reads 0x8000000F/0x104C000F (powered). Swapped-argument
 *   CPU_ON calls hard-freeze EL3 — banned.
 *   Stock kernel boots its 8 cpus through cal_cpu_enable → pmucal
 *   sequences = direct PMC writes (drivers/soc/samsung/cal-if/
 *   pmucal_cpu.c + exynos7885 table).
 *
 * P1.5c: P1.5b + crash forensics kept on screen (red panic stripe,
 * ESR/FAR/STG/BASE printed, no screen clear), runtime load-base print
 * and a 15-cell step-mark trail at y=1464 exposing how far the flow
 * got before any exception.
 *
 * Band meanings (GREEN pass / RED fail / AMBER info / DARK not reached):
 *   0 live FDT in x0        1 PSCI strictly legacy trio (AMBER)
 *   2 upper RAM R/W         3 GIC-400 sanity (read-only)
 *   4 watchdog ok           5 PMC accepted: status 0x2284 flipped to 0xF
 *   6 any observation channel from a secondary (tick/cell)
 *   7 summary (green iff bands 0..6 all green/amber)
 */

#include "console.h"
#include "loader.h"

#define GICD_BASE	0x12301000UL	/* fdt gic node: GIC-400 */
#define GICC_BASE	0x12302000UL
#define WDT_BASE	0x10060000UL	/* /watchdog@10060000 */
#define RAM_UPPER	0x880000000UL	/* 2 GiB upper bank */

#define PSCI_VERSION		0x84000000UL
#define PSCI_CPU_SUSPEND	0xc4000001UL
#define PSCI_CPU_OFF		0x84000002UL
#define PSCI_CPU_ON		0xc4000003UL
#define SMCCC_VERSION		0x80000000UL
#define NOT_SUPPORTED		0xFFFFFFFFUL	/* legacy PSCI 0.2- */
#define PSCI_ALREADY_ON		0xFFFFFFFCUL	/* DEN0022D: -4 */

#define FDT_MAGIC	0xd00dfeedUL
#define BANK0_HI	0xba600000ULL
#define BANK1_HI	0x100000000ULL
#define BANK2_HI	0x900000000ULL

/* trace channels (start.S writers) */
#define SEC_CELLS	16
#define FB_TICK_Y	1496
#define FB_TICK_X	8

extern volatile uint64_t sec_probe[];

#define rsr(r) ({ uint64_t _v; __asm__ volatile("mrs %0, " r : "=r"(_v)); _v; })
#define isb() __asm__ volatile("isb" ::: "memory")

static inline uint32_t rd32(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t rd32be(uintptr_t a) { return __builtin_bswap32(rd32(a)); }

static const char *stage;	/* runtime strings only: no abs pointers */

/* 16 mpidr-slot cells + 16 guarded x0-slot cells (32 B each;
 * written by start.S secondary_entry — must be global). */
volatile uint64_t sec_probe[SEC_CELLS * 4 * 2];

static uint64_t smc(uint64_t fn, uint64_t a1, uint64_t a2, uint64_t a3,
		    uint64_t *_x1)
{
	register uint64_t x0 __asm__("x0") = fn;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = a3;
	__asm__ volatile("smc #0"
			 : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3)
			 :
			 : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
			   "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	if (_x1)
		*_x1 = x1;
	return x0;
}

static uint64_t now(void)
{
	isb();
	return rsr("cntpct_el0");
}

static void delay_ms(unsigned ms)
{
	uint64_t f = rsr("cntfrq_el0");
	uint64_t end = now() + f / 1000 * ms;
	while (now() < end)
		;
}

/* ---- FDT mini-parser: header + /cpus/cpu-at-node reg (aff value) ---- */

struct fdt_info {
	uint64_t dtb;
	uint32_t magic, totalsize;
	uint64_t cpu_aff[NR_CPUS];
	int ncpu;
};

/* is [a, a+len) inside any known RAM bank? */
static int addr_ok(uint64_t a, uint64_t len)
{
	uint64_t e = a + len;
	if (a >= 0x80000000ULL && e <= BANK0_HI)
		return 1;
	if (a >= 0xc0000000ULL && e <= BANK1_HI)
		return 1;
	if (a >= 0x880000000ULL && e <= BANK2_HI)
		return 1;
	return 0;
}

static void parse_cpus(struct fdt_info *fi)
{
	uintptr_t s11 = fi->dtb;
	uintptr_t stru = s11 + rd32be(s11 + 8);
	uintptr_t strs = s11 + rd32be(s11 + 12);
	uintptr_t p = stru;
	int depth = 0, in_cpus = 0, in_cpu = 0;
	int root_seen = 0;
	uint64_t cur = 0;

	fi->ncpu = 0;
	for (;;) {
		uint32_t tok = rd32be(p);
		p += 4;
		switch (tok) {
		case 0x00000001: {	/* BEGIN_NODE */
			/* Root node has no name: value after it is a
			 * PROP token. Root = depth 0; /cpus at depth 1;
			 * cpu-at nodes at depth 2. */
			if (!root_seen) {
				root_seen = 1;
				break;
			}
			const char *nmp = (const char *)p;
			uintptr_t nq = p;
			while (*(const volatile char *)nq)
				nq++;
			p = (nq + 4) & ~(uintptr_t)3;
			depth++;
			if (depth == 1 && !in_cpus && nmp[0] == 'c' &&
			    nmp[1] == 'p' && nmp[2] == 'u' && nmp[3] == 's' &&
			    nmp[4] == 0)
				in_cpus = 1;
			else if (in_cpus && depth == 2 && !in_cpu &&
				 nmp[0] == 'c' && nmp[1] == 'p' &&
				 nmp[2] == 'u' && nmp[3] == '@') {
				in_cpu = 1;
				cur = 0;
			}
			break;
		}
		case 0x00000002:	/* END_NODE */
			if (in_cpu && depth == 2 && fi->ncpu < NR_CPUS) {
				fi->cpu_aff[fi->ncpu++] = cur;
				in_cpu = 0;
			}
			if (depth == 1 && in_cpus)
				in_cpus = 0;
			if (depth == 0)
				return;	/* root closed */
			depth--;
			break;
		case 0x00000003: {	/* PROP: len, nameoff, value... */
			uint32_t len = rd32be(p), name_off = rd32be(p + 4);
			const char *nmp = (const char *)(strs + name_off);
			p += 8;
			if (in_cpu && depth == 2 && nmp[0] == 'r' &&
			    nmp[1] == 'e' && nmp[2] == 'g' && nmp[3] == 0) {
				if (len == 4)
					cur = rd32be(p) & 0xffffff;
				else if (len == 8)
					cur = rd32be(p + 4) & 0xffffff;
			}
			p += (len + 3) & ~3u;
			break;
		}
		case 0x00000004:	/* NOP */
			break;
		case 0x00000009:	/* END */
			return;
		}
	}
}

/*
 * PSCI CPU_ON target (EL1 entries only; the asm stub records ctx/EL/
 * mpidr into sec_probe before calling us, plus paints its fb tick).
 */
void secondary_main(uint64_t idx)
{
	(void)idx;
	for (;;)
		__asm__ volatile("wfe");
}

/* ---- exceptions ---- */

void exc_handler(uint64_t slot, uint64_t *regs)
{
	uintptr_t far_, esr, base;

	(void)regs;
	if (slot == 5)	/* IRQ: none should fire; ignore if stray */
		return;
	base = (uintptr_t)&sec_probe;
	__asm__ volatile("mrs %0, esr_el1" : "=r"(esr));
	__asm__ volatile("mrs %0, far_el1" : "=r"(far_));

	/* keep ALL previous screen content: the step-mark trail and
	 * every printed value must survive for diagnosis. Red stripe
	 * marks the panic; panic values overwrite the first rows. */
	con_mark(0, 0, 720, 36, RED);
	con_cursor(1);
	con_str("PANIC", "unexpected exception");
	con_hex2("SLOT", "idx=", (uint32_t)slot & 7, "cpu=",
		 (uint32_t)rsr("mpidr_el1"));
	con_hex2("ESR", "hi=", (uint32_t)(esr >> 32), "lo=",
		 (uint32_t)esr);
	con_hex2("FAR", "hi=", (uint32_t)(far_ >> 32), "lo=",
		 (uint32_t)far_);
	con_hex2(" CUR", "ptr=", (uint32_t)(uintptr_t)stage, "lo=",
		 (uint32_t)((uint64_t)(uintptr_t)stage >> 32));
	con_hex2(" BASE", "hi=", 0, "lo=", (uint32_t)base);
	for (;;)
		__asm__ volatile("wfe");
}

static uint32_t mpidr_slot(uint64_t mpidr)
{
	return (uint32_t)((mpidr & 3) + 4 * ((mpidr >> 8) & 3));
}

/* ---- Exynos7885-family PMU per-CPU power regs (stock cal-if table) ----
 * PMU base 0x11c80000 (fdt exynos-pmu syscon reg). Configuration at
 * +0, status at +4, stride 0x80 per CPU inside a cluster block:
 *   C0: 0x2000, 0x2080   | C1: 0x2200, 0x2280, 0x2300, 0x2380
 *   C2: 0x2100, 0x2180
 * Boot CPU (mpidr 0x80000100) expected at C1_CPU0 (0x2200).
 * Idle table: CONFIG = 0xF powers a core on, 0x0 shuts it down.
 */
#define PMU_BASE	0x11C80000UL

static const unsigned pmu_core_off[8] = {
	0x2000,	0x2080,	0x2200,	0x2280,	0x2300,	0x2380,	0x2100,	0x2180,
};

/* 16-value PMU snapshot (per-core cfg/st only — the full 64 KiB page
 * scan panicked P1.5, likely unmapped holes in the PMU window) */
#define PMU_WORDS	16
static uint32_t pm_snap[PMU_WORDS];

static void pm_snap_full(void)
{
	int k;

	for (k = 0; k < 8; k++) {
		pm_snap[k * 2] = rd32(PMU_BASE + pmu_core_off[k]);
		pm_snap[k * 2 + 1] = rd32(PMU_BASE + pmu_core_off[k] + 4);
	}
}

static void pm_dump(int also_snap)
{
	int k;
	for (k = 0; k < 8; k++) {
		char lab[8];
		uint32_t conf = rd32(PMU_BASE + pmu_core_off[k]);
		uint32_t st = rd32(PMU_BASE + pmu_core_off[k] + 4);

		if (also_snap) {
			pm_snap[k * 2] = conf;
			pm_snap[k * 2 + 1] = st;
		}
		lab[0] = ' '; lab[1] = 'C'; lab[2] = (char)('0' + (k % 8));
		lab[3] = 0;
		/* only powered cores listed: keeps the readout short
		 * (at shim boot that is the boot core alone) */
		if ((conf & 0xF) == 0xF)
			con_hex3(lab, "cfg=", conf, "stv=", st,
				 "->", pmu_core_off[k]);
	}
	con_hex2("PMBASE", "at=", PMU_BASE, "seqn=", rd32(PMU_BASE + 0x2484));
}

static void pm_delta(void)
{
	int k, n_chg = 0;

	for (k = 0; k < 8; k++) {
		uint32_t conf = rd32(PMU_BASE + pmu_core_off[k]);
		uint32_t st = rd32(PMU_BASE + pmu_core_off[k] + 4);
		char lab[8];

		if (conf == pm_snap[k * 2] && st == pm_snap[k * 2 + 1])
			continue;
		lab[0] = ' '; lab[1] = 'D'; lab[2] = (char)('0' + (k % 8));
		lab[3] = 0;
		con_hex3(lab, "fo=", pm_snap[k * 2], "fs=",
			 pm_snap[k * 2 + 1], "now=", conf);
		con_hex3(lab, "fs2=", pm_snap[k * 2 + 1], "nows=",
			 st, 0, 0);
		n_chg++;
	}
	con_hex2("PMCHG", "n=", n_chg, 0, 0);
}

void shim_main(uint64_t dtb, uint64_t boot_el, uint64_t boot_mpidr)
{
	struct fdt_info fi;
	uint32_t t;

	(void)GICC_BASE;
	(void)PSCI_CPU_SUSPEND;
	(void)PSCI_CPU_OFF;
	(void)PSCI_CPU_ON;
	(void)NOT_SUPPORTED;
	(void)PSCI_ALREADY_ON;

	stage = "bands";
	con_ytop(NBANDS * 36);
	con_clear(DARK);
	band_reset();
	con_str("SHIM", "a30s probe P1.5c");
	con_hex2("ENTRY", "el=", (uint32_t)boot_el, "mpidr=",
		 (uint32_t)boot_mpidr);
	con_hex2("BASE", "hi=", 0, "lo=", (uint32_t)(uintptr_t)&sec_probe);
	con_mark(1464, 8, 6, 12, WHITE);		/* step 0: entered */
	con_skip();

	/* P2.2: try to boot an XNU payload first (embedded channel,
	 * then the SBOOT initrd channel). On any failure the P1 probe
	 * screen below still runs as diagnostics. */
	stage = "loader";
	{
		const char *ler = loader_boot(dtb);

		if (ler)
			con_str("PLFAIL", ler);
		else
			return;		/* jumped into xnu; never returns */
	}
	con_skip();

	/* band 0: live FDT */
	stage = "fdt";
	fi.dtb = dtb;
	fi.magic = 0;
	fi.totalsize = 0;
	fi.ncpu = 0;
	if (addr_ok(dtb, 0x20)) {
		t = rd32be(dtb);
		fi.magic = t;
		if (t == FDT_MAGIC) {
			fi.totalsize = rd32be(dtb + 4);
			if (fi.totalsize > 0x20 && fi.totalsize < 0x1000000)
				parse_cpus(&fi);
		}
	}
	band_set(0, fi.magic == FDT_MAGIC && fi.totalsize &&
		     fi.ncpu > 0 ? GREEN : RED);
	con_hex2("DTB", "at=", (uint32_t)dtb, "hi=", (uint32_t)(dtb >> 32));
	con_hex2("FDT", "magic=", fi.magic, "total=", fi.totalsize);
	con_hex2("CPUS", "n=", fi.ncpu, "self=", (uint32_t)boot_mpidr);
	con_mark(1464, 18, 6, 12, GREEN);		/* step 1: fdt done */

	/* band 1: PSCI (AMBER expected: legacy trio, no version call) */
	stage = "psci";
	t = (uint32_t)smc(PSCI_VERSION, 0, 0, 0, 0);
	band_set(1, (t && t != NOT_SUPPORTED) ? GREEN : AMBER);
	con_hex2("PSCI", "ver=", t, "smccc=",
		 (uint32_t)smc(SMCCC_VERSION, 0, 0, 0, 0));
	con_mark(1464, 28, 6, 12, GREEN);		/* step 2: psci done */

	/* band 2: upper RAM bank R/W, cacheless */
	stage = "ram";
	{
		volatile uint64_t *ram = (void *)RAM_UPPER;
		uint64_t bad = 0;
		unsigned i;

		for (i = 0; i < 4096; i++)
			ram[i] = 0x58514751ULL * (i + 1);
		for (i = 0; i < 4096; i++)
			if (ram[i] != 0x58514751ULL * (i + 1))
				bad++;
		band_set(2, bad == 0 ? GREEN : RED);
		con_hex2("RAM", "bad=", (uint32_t)bad, 0, 0);
	}
	con_mark(1464, 38, 6, 12, GREEN);		/* step 3: ram done */

	/* band 3: GIC-400 sanity, strictly read-only */
	stage = "gicd";
	{
		uint32_t ctlr = rd32(GICD_BASE);
		uint32_t typer = rd32(GICD_BASE + 4);
		uint32_t nint = ((typer & 0x1f) + 1) * 32;
		band_set(3, nint >= 32 && nint <= 1024 ? GREEN : RED);
		con_hex2("GICD", "ctlr=", ctlr, "typer=", typer);
		con_hex2("IRQS", "n=", nint, 0, 0);
	}
	con_mark(1464, 48, 6, 12, GREEN);		/* step 4: gic done */

	/* band 4: Exynos watchdog */
	stage = "wdt";
	wr32(WDT_BASE, 0);
	band_set(4, rd32(WDT_BASE) == 0 ? GREEN : RED);
	con_hex2("WDT", "con=", rd32(WDT_BASE), 0, 0);
	{
		uint32_t fq = (uint32_t)rsr("cntfrq_el0");
		con_hex2("FRQ", "hz=", fq, 0, 0);
	}
	con_mark(1464, 58, 6, 12, GREEN);		/* step 5: wdt done */
	con_skip();

	/* PMU per-core power state (read-only), then full-page snapshot
	 * base for the post-experiment diff */
	stage = "pmu";
	pm_dump(1);
	pm_snap_full();
	con_mark(1464, 68, 6, 12, GREEN);		/* step 6: pmu snapshot */
	con_skip();

	/* band 5+6: MANUAL Exynos-PMC secondary power-on — no SMC at
	 * all (the TC CPU_ON handler is a proven no-op and unknown
	 * calls risk the P1.3-style firmware freeze). Sequence per
	 * stock pmucal_cpu_exynos7885.h: CLUSTER1 per-core on needs
	 * ONLY cfg |= 0xF. PRE marker paints BLUE at y=294 x=24, POST
	 * overpaints WHITE (status 0xF reached) / RED (no flip). */
	stage = "pmcon";
	{
		uint32_t cfg, post_cfg, post_st;
		int ok = 0;

		cfg = rd32(PMU_BASE + 0x2280);
		con_mark(1464, 78, 6, 12, BLUE);	/* step 7: pre-write */
		wr32(PMU_BASE + 0x2280, cfg | 0xF);
		con_mark(1464, 88, 6, 12, WHITE);	/* step 8: written */
		{
			uint64_t f = rsr("cntfrq_el0");
			uint64_t end = now() + f / 1000 * 1500;

			while (now() < end) {
				if ((rd32(PMU_BASE + 0x2284) & 0xF) == 0xF) {
					ok = 1;
					break;
				}
			}
		}
		post_cfg = rd32(PMU_BASE + 0x2280);
		post_st = rd32(PMU_BASE + 0x2284);
		con_mark(1464, 98, 6, 12, ok ? GREEN : RED); /* step 9: poll */
		band_set(5, ok ? GREEN : RED);
		con_hex3(" SC1P", "cfg=", post_cfg, "stv=", post_st,
			 "pre=", cfg);
		con_mark(1464, 108, 6, 12, AMBER);	/* step 10: SC1P out */
		/* observation window: sec channels, late PMC flips */
		delay_ms(800);
		con_mark(1464, 118, 6, 12, GREEN);	/* step 11: win done */

		/* band 6: union of observation channels */
		{
			int n_ack = 0, s;
			uint32_t tickbits = 0;

			/* mpidr-slot cells, nonzero only */
			for (s = 0; s < SEC_CELLS; s++) {
				char lab[8];

				if (!sec_probe[s * 4 + 2])
					continue;
				lab[0] = ' '; lab[1] = 'M'; lab[2] = 'P';
				lab[3] = (char)('0' + (s % 10)); lab[4] = 0;
				con_hex3(lab, "el=",
					 (uint32_t)(sec_probe[s * 4 + 1] >> 2),
					 "x0=", (uint32_t)sec_probe[s * 4],
					 "mdr=", (uint32_t)sec_probe[s * 4 + 2]);
				n_ack++;
			}
			/* x0-slot cells: print only for cores whose
			 * mpidr channel did NOT land */
			for (s = 0; s < SEC_CELLS; s++) {
				char lab[8];
				uint32_t mtx;

				if (!sec_probe[SEC_CELLS * 4 + s * 4 + 2])
					continue;
				mtx = mpidr_slot(
					sec_probe[SEC_CELLS * 4 + s * 4 + 2]);
				if (sec_probe[mtx * 4 + 2])
					continue;
				lab[0] = ' '; lab[1] = 'C'; lab[2] = 'S';
				lab[3] = (char)('0' + (s % 10)); lab[4] = 0;
				con_hex3(lab, "el=",
					 (uint32_t)(sec_probe[SEC_CELLS * 4 +
							    s * 4 + 1] >> 2),
					 "x0=",
					 (uint32_t)sec_probe[SEC_CELLS * 4 +
							     s * 4],
					 "mdr=",
					 (uint32_t)sec_probe[SEC_CELLS * 4 +
							     s * 4 + 2]);
				n_ack++;
			}
			/* fb ticks seen by the boot CPU through RAM */
			for (s = 0; s < SEC_CELLS; s++) {
				uint32_t px = con_fb[FB_TICK_Y * FB_W +
						     FB_TICK_X + s * 8];

				if (px == 0xffffffff)
					tickbits |= 1u << s;
				else if (px != DARK)
					tickbits |= 0x80000000u | 1u << s;
			}
			con_hex2("TICKS", "bits=", tickbits,
				 "n=", __builtin_popcount(tickbits &
							  0x0000ffff));
			if (__builtin_popcount(tickbits & 0xffff))
				n_ack++;
			band_set(6, n_ack ? GREEN : RED);
			con_hex2("CORES", "on=", (uint32_t)ok, "ack=", n_ack);
		}
		con_mark(1464, 128, 6, 12, GREEN);	/* step 12: chans */
		/* post-run PMU delta: every changed register between the
		 * snapshot and now (PMC + anything async) */
		stage = "pmdelta";
		pm_delta();
		con_mark(1464, 138, 6, 12, GREEN);	/* step 13: delta */
	}

	/* band 7: summary: green if every consulted band is green/amber */
	stage = "halt";
	{
		int k, good = 1;
		uint32_t got = 0;
		for (k = 0; k < 7; k++) {
			uint32_t y = BAND_OF_Y(k) * (FB_STRIDE / 4);
			got = con_fb[y];
			if (got != GREEN && got != AMBER)
				good = 0;
		}
		if (good)
			band_set(7, GREEN);
	}
	con_skip();
	con_str("DONE", "halt; hold power to restart");
	con_mark(1464, 148, 6, 12, GREEN);		/* step 14: complete */
	for (;;)
		__asm__ volatile("wfe");
}
