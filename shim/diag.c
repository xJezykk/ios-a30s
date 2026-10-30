/*
 * Read-only A30s handoff diagnostic shim. This is built separately from
 * a30s.c so inspecting kernel boot prerequisites never runs the P1 PMC
 * experiment, RAM test, or watchdog write.
 */
#include "console.h"

#define DWMMC0_BASE	0x13500000UL
#define DWMMC_CTRL	0x000
#define DWMMC_PWREN	0x004
#define DWMMC_CLKDIV	0x008
#define DWMMC_CLKSRC	0x00c
#define DWMMC_CLKENA	0x010
#define DWMMC_TMOUT	0x014
#define DWMMC_CTYPE	0x018
#define DWMMC_STATUS	0x048
#define DWMMC_FIFOTH	0x04c
#define DWMMC_UHS_REG	0x074
#define DWMMC_CLKSEL_32BIT_DESC	0x09c
#define DWMMC_CLKSEL_64BIT_DESC	0x0a8
#define DWMMC_VERID	0x06c
#define DWMMC_HCON	0x070

#define FDT_MAGIC	0xd00dfeedU
#define FDT_BEGIN_NODE	0x00000001U
#define FDT_END_NODE	0x00000002U
#define FDT_PROP	0x00000003U
#define FDT_NOP		0x00000004U
#define FDT_END		0x00000009U

#define RAM0_END	0xba600000ULL
#define RAM1_END	0x100000000ULL
#define RAM2_END	0x900000000ULL
#define SEC_CELLS	16

#define MRS(reg) ({ uint64_t v; __asm__ volatile("mrs %0, " #reg : "=r"(v)); v; })

volatile uint64_t sec_probe[SEC_CELLS * 4 * 2];

static inline uint32_t rd32(uintptr_t a)
{
	return *(volatile const uint32_t *)a;
}

static inline uint32_t rd32be(uintptr_t a)
{
	return __builtin_bswap32(rd32(a));
}

static int in_ram(uint64_t a, uint64_t len)
{
	uint64_t end = a + len;

	if (end < a)
		return 0;
	return (a >= 0x80000000ULL && end <= RAM0_END) ||
	       (a >= 0xc0000000ULL && end <= RAM1_END) ||
	       (a >= 0x880000000ULL && end <= RAM2_END);
}

static int span_ok(uint32_t off, uint32_t len, uint32_t total)
{
	return off <= total && len <= total - off;
}

static int name_is(const char *name, uint32_t maxlen, const char *want)
{
	uint32_t i = 0;

	while (i < maxlen && name[i] && want[i] && name[i] == want[i]) {
		i++;
	}
	return i < maxlen && name[i] == 0 && want[i] == 0;
}

struct fdt_summary {
	uint32_t total, struct_off, strings_off, struct_size, strings_size;
	uint32_t cpus, memories;
	uint64_t initrd_start, initrd_end;
	uint64_t memory_base, memory_size;
	int has_initrd_start, has_initrd_end;
	int has_memory;
};

static uint64_t fdt_cells(uintptr_t dtb, uint32_t pos, uint32_t count)
{
	uint64_t value = rd32be(dtb + pos);

	if (count == 2)
		value = (value << 32) | rd32be(dtb + pos + 4);
	return value;
}

static int inspect_fdt(uintptr_t dtb, struct fdt_summary *out)
{
	uint32_t pos, depth = 0;
	uint8_t chosen[32] = { 0 };
	uint8_t cpus_node[32] = { 0 };
	uint8_t cpu_node[32] = { 0 };
	uint8_t cpu_counted[32] = { 0 };
	uint8_t memory_node[32] = { 0 };
	uint32_t address_cells = 2, size_cells = 1;
	uint32_t total = rd32be(dtb + 4);
	uint32_t struct_off = rd32be(dtb + 8);
	uint32_t strings_off = rd32be(dtb + 12);
	uint32_t strings_size = rd32be(dtb + 32);
	uint32_t struct_size = rd32be(dtb + 36);

	if (rd32be(dtb) != FDT_MAGIC || total < 40 || total > 0x1000000 ||
	    !span_ok(struct_off, struct_size, total) ||
	    !span_ok(strings_off, strings_size, total) ||
	    struct_size < 4 || !in_ram(dtb, total))
		return 0;

	out->total = total;
	out->struct_off = struct_off;
	out->strings_off = strings_off;
	out->struct_size = struct_size;
	out->strings_size = strings_size;
	out->cpus = 0;
	out->memories = 0;
	out->initrd_start = 0;
	out->initrd_end = 0;
	out->has_initrd_start = 0;
	out->has_initrd_end = 0;
	out->memory_base = 0;
	out->memory_size = 0;
	out->has_memory = 0;
	pos = struct_off;

	while (span_ok(pos, 4, struct_off + struct_size)) {
		uint32_t token = rd32be(dtb + pos);

		pos += 4;
		switch (token) {
		case FDT_BEGIN_NODE: {
			uint32_t name_pos = pos;
			uint32_t limit = struct_off + struct_size;
			uint32_t name_len;
			const char *node;
			int parent_chosen = depth ? chosen[depth - 1] : 0;
			int parent_cpus = depth ? cpus_node[depth - 1] : 0;
			int is_memory;

			while (pos < limit && *(volatile const uint8_t *)(dtb + pos))
				pos++;
			if (pos == limit || depth >= 32)
				return 0;
			name_len = pos - name_pos;
			node = (const char *)(dtb + name_pos);
			is_memory = name_is(node, name_len, "memory") ||
				(name_len > 7 && node[0] == 'm' &&
				 node[1] == 'e' && node[2] == 'm' &&
				 node[3] == 'o' && node[4] == 'r' &&
				 node[5] == 'y' && node[6] == '@');
			chosen[depth] = parent_chosen ||
				(depth == 1 && name_is(node, name_len, "chosen"));
			cpus_node[depth] = depth == 1 &&
				name_is(node, name_len, "cpus");
			cpu_node[depth] = depth == 2 && parent_cpus;
			cpu_counted[depth] = 0;
			memory_node[depth] = depth == 1 && is_memory;
			if (is_memory)
				out->memories++;
			pos = (pos + 4) & ~3U;
			depth++;
			break;
		}
		case FDT_END_NODE:
			if (!depth)
				return 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len, nameoff;
			uint32_t padded;
			const char *prop;
			uint32_t maxlen;

			if (depth >= 32 || !span_ok(pos, 8, struct_off + struct_size))
				return 0;
			len = rd32be(dtb + pos);
			nameoff = rd32be(dtb + pos + 4);
			pos += 8;
			padded = (len + 3) & ~3U;
			if (padded < len ||
			    !span_ok(pos, padded, struct_off + struct_size) ||
			    nameoff >= strings_size)
				return 0;
			prop = (const char *)(dtb + strings_off + nameoff);
			maxlen = strings_size - nameoff;
			if (depth == 1 && len == 4 &&
			    name_is(prop, maxlen, "#address-cells"))
				address_cells = rd32be(dtb + pos);
			else if (depth == 1 && len == 4 &&
				 name_is(prop, maxlen, "#size-cells"))
				size_cells = rd32be(dtb + pos);
			else if (depth == 3 && cpu_node[depth - 1] &&
				 !cpu_counted[depth - 1] && len == 4 &&
				 name_is(prop, maxlen, "device_type") &&
				 *(volatile const uint32_t *)(dtb + pos) ==
				 0x00757063U) {
				out->cpus++;
				cpu_counted[depth - 1] = 1;
			}
			else if (depth == 2 && memory_node[depth - 1] &&
				 name_is(prop, maxlen, "reg") &&
				 address_cells >= 1 && address_cells <= 2 &&
				 size_cells >= 1 && size_cells <= 2 &&
				 len >= (address_cells + size_cells) * 4) {
				out->memory_base =
					fdt_cells(dtb, pos, address_cells);
				out->memory_size =
					fdt_cells(dtb,
						  pos + address_cells * 4,
						  size_cells);
				out->has_memory = 1;
			}
			if (chosen[depth] && name_is(prop, maxlen,
						     "linux,initrd-start")) {
				if (len == 4) {
					out->initrd_start = rd32be(dtb + pos);
					out->has_initrd_start = 1;
				} else if (len == 8) {
					out->initrd_start =
						((uint64_t)rd32be(dtb + pos) << 32) |
						rd32be(dtb + pos + 4);
					out->has_initrd_start = 1;
				}
			} else if (chosen[depth] &&
				   name_is(prop, maxlen, "linux,initrd-end")) {
				if (len == 4) {
					out->initrd_end = rd32be(dtb + pos);
					out->has_initrd_end = 1;
				} else if (len == 8) {
					out->initrd_end =
						((uint64_t)rd32be(dtb + pos) << 32) |
						rd32be(dtb + pos + 4);
					out->has_initrd_end = 1;
				}
			}
			pos += padded;
			break;
		}
		case FDT_NOP:
			break;
		case FDT_END:
			return depth == 0;
		default:
			return 0;
		}
	}
	return 0;
}

void secondary_main(uint64_t idx)
{
	(void)idx;
	for (;;)
		__asm__ volatile("wfe");
}

void exc_handler(uint64_t slot, uint64_t *regs)
{
	uint64_t esr, far;

	(void)regs;
	__asm__ volatile("mrs %0, esr_el1" : "=r"(esr));
	__asm__ volatile("mrs %0, far_el1" : "=r"(far));
	con_str("DIAG", "exception; halted");
	con_hex2("SLOT", "idx=", (uint32_t)slot, "mpidr=",
		 (uint32_t)MRS(mpidr_el1));
	con_hex("ESR", esr);
	con_hex("FAR", far);
	for (;;)
		__asm__ volatile("wfe");
}

void shim_main(uint64_t dtb, uint64_t boot_el, uint64_t boot_mpidr)
{
	struct fdt_summary fdt;

	con_ytop(NBANDS * 36);
	con_clear(DARK);
	con_str("BUILD", "DIAG7-MIN");
	con_hex("EL", boot_el);
	con_hex("MPIDR", boot_mpidr);
	con_hex("CPU", MRS(midr_el1));
	con_hex("TIMER", MRS(cntfrq_el0));
	con_hex2("MMU", "sctlr=", (uint32_t)MRS(sctlr_el1), "tcr=",
		 (uint32_t)MRS(tcr_el1));
	con_hex("DTB", dtb);

	if (in_ram(dtb, 40) && inspect_fdt(dtb, &fdt)) {
		con_hex2("FDT", "cpu=", fdt.cpus, "mem=", fdt.memories);
		if (fdt.has_memory) {
			con_hex("RAM BASE", fdt.memory_base);
			con_hex("RAM SIZE", fdt.memory_size);
		}
		if (fdt.has_initrd_start && fdt.has_initrd_end) {
			con_hex("IRD LO", fdt.initrd_start);
			con_hex("IRD HI", fdt.initrd_end);
		} else {
			con_str("INITRD", "none");
		}
	} else {
		con_str("FDT", "invalid");
	}

	con_hex2("MMC", "ctrl=", rd32(DWMMC0_BASE + DWMMC_CTRL), "pwr=",
		 rd32(DWMMC0_BASE + DWMMC_PWREN));
	con_hex2("CLK", "div=", rd32(DWMMC0_BASE + DWMMC_CLKDIV), "ena=",
		 rd32(DWMMC0_BASE + DWMMC_CLKENA));
	con_hex2("SRC", "src=", rd32(DWMMC0_BASE + DWMMC_CLKSRC), "to=",
		 rd32(DWMMC0_BASE + DWMMC_TMOUT));
	con_hex2("BUS", "type=", rd32(DWMMC0_BASE + DWMMC_CTYPE), "stat=",
		 rd32(DWMMC0_BASE + DWMMC_STATUS));
	con_hex2("FIFO", "th=", rd32(DWMMC0_BASE + DWMMC_FIFOTH), "uhs=",
		 rd32(DWMMC0_BASE + DWMMC_UHS_REG));
	con_hex2("CSEL", "9c=", rd32(DWMMC0_BASE + DWMMC_CLKSEL_32BIT_DESC),
		 "a8=", rd32(DWMMC0_BASE + DWMMC_CLKSEL_64BIT_DESC));
	con_hex2("ID", "hcon=", rd32(DWMMC0_BASE + DWMMC_HCON), "ver=",
		 rd32(DWMMC0_BASE + DWMMC_VERID));
	con_str("END", "read-only; halted");
	for (;;)
		__asm__ volatile("wfe");
}
