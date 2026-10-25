/*
 * P2.2: boot an XNU payload (the mkpayload.py "SWEETXNU" blob), A30s
 * edition. This is the port of the upstream ios-sweet loader.c with
 * the Exynos changes of this shim:
 *
 *   - payload discovery has two channels, embedded first:
 *       1. embedded: build.sh appends the payload to this Image and
 *          patches a linker-script info block (payload_info) - the
 *          payload position is computed from the RUNTIME address of
 *          that block, so SBOOT's drifting load base never matters
 *          (P1.5c evidence: base moved per boot);
 *       2. FDT initrd: the upstream path - SBOOT passes a ramdisk in
 *          /chosen (linux,initrd-start/end) and the payload rides
 *          there as the boot image ramdisk. Kept as fallback because
 *          the P1 recon showed SBOOT's initrd pointing at its own
 *          0x89000000 vendor ramdisk, not necessarily ours: the
 *          SWEETXNU magic check is the arbiter;
 *   - Video constants are the A30s panel from console.h
 *     (SBOOT leaves it running: s3cfb.bootloaderfb=0xEC000000);
 *   - diagnostics go through this shim's con_* (bands + text), and a
 *     failure returns so the P1 probe can run as the diagnostic
 *     screen;
 *   - the boot contract is unchanged from upstream: MMU and caches
 *     off, x0 = boot_args physical address, Revision/Version 2,
 *     deviceTreeP = ADT virtual address (xnu reads it with the MMU
 *     on after it maps [physBase..+memSize) cacheable).
 */
#include "a30s.h"
#include "console.h"
#include "fdt.h"
#include "loader.h"

#define BOOT_LINE_LENGTH	608
#define PAGE_16K		0x4000UL

#define FDT_MAGIC		0xD00DFEEDU
#define FDT_BEGIN_NODE		0x00000001U
#define FDT_END_NODE		0x00000002U
#define FDT_PROP		0x00000003U
#define FDT_NOP			0x00000004U

/* magic for the embedded-payload info block, must match build.sh:
 * bytes "A30P", little endian */
#define PINFO_MAGIC		0x50303341U

#define LD_BE32(a)	__builtin_bswap32(*(volatile const uint32_t *)(a))

struct payload_seg {
	uint64_t vmaddr, vmsize, file_off, file_size;
};

struct payload {
	char magic[8];
	uint32_t version, nsegs;
	uint64_t virt_base, entry_va, adt_off, adt_size, kernel_end;
	uint64_t phys_base, mem_size;
	char cmdline[BOOT_LINE_LENGTH];
	struct payload_seg segs[];
};

/* payload_info is allocated by shim.ld; build.sh patches it */
struct payload_info {
	uint32_t magic, flags;
	uint64_t off, size;	/* payload = (uintptr_t)&payload_info + off */
};
extern const struct payload_info payload_info;

/* pexpert/pexpert/arm64/boot.h, Revision/Version 2 */
struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t Revision, Version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video Video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char CommandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags, memSizeActual;
};

_Static_assert(__builtin_offsetof(struct payload, segs) == 0x2a8,
	       "payload header layout");
_Static_assert(__builtin_offsetof(struct boot_args, Video) == 40,
	       "boot_args layout");
_Static_assert(__builtin_offsetof(struct boot_args, CommandLine) == 108,
	       "boot_args layout");
_Static_assert(sizeof(struct boot_args) == 736, "boot_args layout");

/* MMU off: everything is Device memory, so copy in aligned 8-byte words */
static void copy64(uintptr_t dst, uintptr_t src, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	volatile const uint64_t *s = (volatile const uint64_t *)src;

	for (uint64_t i = 0; i < (n + 7) / 8; i++)
		d[i] = s[i];
}

static void zero64(uintptr_t dst, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;

	for (uint64_t i = 0; i < n / 8; i++)
		d[i] = 0;
}

static int magic_ok(const char *m)
{
	static const char want[] = "SWEETXNU";

	for (int i = 0; i < 8; i++)
		if (m[i] != want[i])
			return 0;
	return 1;
}

static int name_eq(const char *a, const char *b)
{
	while (*b) {
		if (*a++ != *b++)
			return 0;
	}
	return *a == 0;
}

/* /chosen linux,initrd-start/end: tolerant of 1-cell (32-bit) and
 * 2-cell (hi,lo) property encodings. 0 on not-found. */
static int fdt_chosen_u64(uintptr_t dtb, const char *want, uint64_t *out)
{
	uintptr_t stru, strs, p;
	uintptr_t end;
	int depth = 0, in_chosen = 0, root_seen = 0;

	if (LD_BE32(dtb) != FDT_MAGIC)
		return 0;
	uint32_t total = LD_BE32(dtb + 4);
	if (total < 40 || total > 0x1000000)
		return 0;
	uint32_t off_struct = LD_BE32(dtb + 8);
	uint32_t size_struct = LD_BE32(dtb + 36);
	if (off_struct > total || size_struct > total - off_struct)
		return 0;
	stru = dtb + off_struct;
	end = stru + size_struct;
	strs = dtb + LD_BE32(dtb + 12);
	if (stru < dtb || strs < dtb || stru > end || strs >= dtb + total)
		return 0;
	p = stru;
	for (;;) {
		if (p > end || end - p < 4)
			return 0;
		uint32_t tok = LD_BE32(p);

		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			/* root node has no name: the value after it is
			 * a PROP token */
			if (!root_seen) {
				root_seen = 1;
				p = fdt_skip_node_name(p, end);
				if (!p)
					return 0;
				break;
			}
			const char *nmp = (const char *)p;
			p = fdt_skip_node_name(p, end);
			if (!p)
				return 0;
			depth++;
			in_chosen = depth == 1 && name_eq(nmp, "chosen");
			break;
		}
		case FDT_END_NODE:
			if (in_chosen && depth == 1)
				in_chosen = 0;
			if (depth == 0)
				return 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = LD_BE32(p);
			uint32_t nameoff = LD_BE32(p + 4);
			const char *prop = (const char *)(strs + nameoff);

			p += 8;
			if (in_chosen && name_eq(prop, want)) {
				if (len == 4) {
					*out = LD_BE32(p);
					return 1;
				}
				if (len == 8) {
					*out = ((uint64_t)LD_BE32(p) << 32) |
					       LD_BE32(p + 4);
					return 1;
				}
				return 0;
			}
			p += (len + 3) & ~(uintptr_t)3;
			break;
		}
		case FDT_NOP:
			break;
		default:
			return 0;
		}
	}
}

static const struct payload *find_payload(uintptr_t dtb, uint64_t *lo,
					  uint64_t *hi)
{
	const struct payload *p;
	uint64_t a, b;

	/* channel 1: payload embedded behind payload_info */
	if (payload_info.magic == PINFO_MAGIC) {
		a = (uintptr_t)&payload_info + payload_info.off;
		p = (const struct payload *)a;
		if (magic_ok(p->magic)) {
			*lo = a;
			*hi = a + payload_info.size;
			con_str("CHAN", "embedded");
			return p;
		}
		con_hex2("EMB", "magic=", LD_BE32((uintptr_t)p), "size=",
			 (uint32_t)payload_info.size);
	}

	/* channel 2: SBOOT ramdisk, as on upstream sweet */
	if (fdt_chosen_u64(dtb, "linux,initrd-start", &a) &&
	    fdt_chosen_u64(dtb, "linux,initrd-end", &b)) {
		con_hex("INITRD lo", a);
		con_hex("INITRD hi", b);
		if (b > a) {
			p = (const struct payload *)a;
			if (magic_ok(p->magic)) {
				*lo = a;
				*hi = b;
				con_str("CHAN", "initrd");
				return p;
			}
			con_hex2("INITRD", "magic=",
				 LD_BE32((uintptr_t)p), 0, 0);
		}
	}
	return NULL;
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);

const char *loader_boot(uint64_t dtb)
{
	uint64_t rd_start, rd_end;
	const struct payload *p = find_payload(dtb, &rd_start, &rd_end);

	if (!p)
		return "no SWEETXNU payload";

	if (p->version != 1 || (p->adt_off & 7) || (rd_start & 7))
		return "bad payload header";

	uint64_t pa_of_va_base = p->phys_base - p->virt_base; /* pa = va + this */

	con_hex("PHYS base", p->phys_base);
	con_hex("PHYS size", p->mem_size);
	con_hex("VIRT base", p->virt_base);
	con_hex("ENTRY va", p->entry_va);
	con_hex2("ADT", "off=", (uint32_t)p->adt_off, "size=",
		 (uint32_t)p->adt_size);
	con_hex2("SEGS", "n=", p->nsegs, 0, 0);

	/* The payload must not overlap where the kernel goes. */
	if (rd_end > p->phys_base && rd_start < p->phys_base + p->mem_size)
		return "ramdisk overlaps kernel memory";

	for (uint32_t i = 0; i < p->nsegs; i++) {
		const struct payload_seg *s = &p->segs[i];
		uintptr_t dst = s->vmaddr + pa_of_va_base;

		copy64(dst, rd_start + s->file_off, s->file_size);
		if (s->vmsize > s->file_size)
			zero64(dst + ((s->file_size + 7) & ~7UL),
			       s->vmsize - ((s->file_size + 7) & ~7UL));
	}
	con_hex("SEGS end", p->kernel_end + pa_of_va_base);

	/* ADT, then boot_args, behind the kernel; everything below
	 * topOfKernelData is ours. */
	uintptr_t adt_pa = p->kernel_end + pa_of_va_base;

	copy64(adt_pa, rd_start + p->adt_off, p->adt_size);
	uintptr_t ba_pa = (adt_pa + p->adt_size + PAGE_16K - 1) & ~(PAGE_16K - 1);
	uintptr_t top = ba_pa + PAGE_16K;

	zero64(ba_pa, PAGE_16K);
	volatile struct boot_args *ba = (volatile struct boot_args *)ba_pa;

	ba->Revision = 2;
	ba->Version = 2;
	ba->virtBase = p->virt_base;
	ba->physBase = p->phys_base;
	ba->memSize = p->mem_size;
	ba->topOfKernelData = top;
	ba->Video.v_baseAddr = FB_BASE;
	ba->Video.v_display = 1;
	ba->Video.v_rowBytes = FB_STRIDE;
	ba->Video.v_width = FB_W;
	ba->Video.v_height = FB_H;
	ba->Video.v_depth = 32;
	ba->deviceTreeP = adt_pa - pa_of_va_base;	/* VA: xnu reads it MMU-on */
	ba->deviceTreeLength = (uint32_t)p->adt_size;
	for (int i = 0; i < BOOT_LINE_LENGTH; i++)
		ba->CommandLine[i] = p->cmdline[i];
	ba->memSizeActual = p->mem_size;
	con_hex("BOOTARG pa", ba_pa);
	con_hex("BOOTARG top", top);

	uintptr_t entry_pa = p->entry_va + pa_of_va_base;

	con_hex("JUMP pa", entry_pa);
	con_str("XNU", "jumping (fbcon clears screen)");

	/* P2.5 root cause, reproduced in QEMU (--sboot-state): SBOOT can enter
	 * this shim with the MMU still on (stale tables) and/or caches running.
	 * The XNU head builds its own page tables assuming a virgin core and
	 * otherwise dies in a prefetch-abort loop on its own head code (FAR =
	 * kernel head, QEMU qsboot run). Hand the core over in the state the
	 * head expects:
	 *   1. dc civac over everything this loader wrote (the one
	 *      cache-coherence hazard QEMU's TCG cannot model);
	 *   2. MMU + D/I caches off, TLB empty, I-cache invalidated. */
	{
		unsigned long a = (unsigned long)p->phys_base, end = (unsigned long)top;

		while (a < end) {
			__asm__ volatile("dc civac, %0" :: "r"(a) : "memory");
			a += 64;
		}
	}
	__asm__ volatile(
	    "dsb sy				\n"
	    "mrs x16, sctlr_el1			\n"
	    "bic x16, x16, #(1 << 0)		\n"	/* MMU off */
	    "bic x16, x16, #(1 << 2)		\n"	/* D-cache off */
	    "bic x16, x16, #(1 << 12)		\n"	/* I-cache off */
	    "msr sctlr_el1, x16			\n"
	    "dsb sy				\n"
	    "tlbi vmalle1			\n"
	    "ic iallu				\n"
	    "dsb sy				\n"
	    "isb				\n"
	    "mov x0, %[ba]			\n"
	    "br %[entry]			\n"
	    :: [ba] "r"(ba_pa), [entry] "r"(entry_pa) : "memory", "x0", "x16");
	return "xnu returned";
}
