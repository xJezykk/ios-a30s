/*
 * a30s boot shim entry. Mirrors ios-sweet/shim/start.S (MIT) with the
 * Samsung-specific bits added: SBOOT's entry privilege level is
 * unknown, so the code drops EL2 down to EL1 before running the C
 * probe. x0 (the live FDT pointer) is preserved across the drop.
 * The IMAGE header follows the Linux arm64 protocol so SBOOT treats us
 * like a kernel: we are loaded where the stock kernel loads (0x10008000)
 * with no relocation pass (runs wherever it lands).
 */
	.section .text.head, "ax"
	.globl _head
_head:
	b	_start			/* code0 */
	.long	0			/* code1 */
	.quad	0			/* text_offset */
	.quad	_end			/* image_size (linked at 0) */
	.quad	0xa			/* flags: LE, 4K pages, place anywhere */
	.quad	0			/* res2 */
	.quad	0			/* res3 */
	.quad	0			/* res4 */
	.ascii	"ARM\x64"		/* magic */
	.long	0			/* res5 (PE offset) */
	/* 0x40 - 0x7f: reserved. Samsung SBOOT may enter the image at
	 * load+0x80 instead of load+0 (Qualcomm ABL convention), so keep
	 * a duplicate entry branch there: both conventions work. */
	.org	0x80
	b	_start
	.balign	8

	.text
	.globl _start
_start:
	msr	daifset, #0xf
	mov	x19, x0			/* DTB pointer from SBOOT */
	mrs	x20, CurrentEL		/* 0x04=EL1, 0x08=EL2, 0x0c=EL3 */
	mrs	x21, mpidr_el1		/* boot cpu id */
	cmp	x20, #0x8
	b.eq	2f
	cmp	x20, #0xc
	b.eq	1f
	b	3f
1:	/* EL3: nothing sane to do at EL3; drop to EL1 the same way. */
2:	/* EL2: handoff to EL1 (AArch64, DAIF masked). */
	mov	x3, xzr
	msr	hcr_el2, x3
	msr	cptr_el2, x3
	mov	x3, #3
	msr	cnthctl_el2, x3
	isb
	adr	x3, el1_entry
	msr	elr_el2, x3
	mov	x3, #0x3c5		/* PSTATE: EL1h, DAIF = 0b111 */
	msr	spsr_el2, x3
	eret
3:	mov	x20, x20		/* normalize EL info (EL1 case) */

el1_entry:
	msr	daifset, #0xf
	msr	spsel, #1
	adr	x1, _stack_top
	mov	sp, x1
	adr	x1, vectors
	msr	vbar_el1, x1
	isb
	/* zero .bss */
	adr	x1, _bss_start
	adr	x2, _bss_end
1:	cmp	x1, x2
	b.hs	2f
	str	xzr, [x1], #8
	b	1b
2:	/* call shim_main(dtb, entry_el, boot_mpidr) */
	mov	x0, x19
	mov	x1, x20
	mov	x2, x21
	bl	shim_main
3:	wfe
	b	3b

/*
 * PSCI CPU_ON target; x0 = whatever the firmware delivers (nominally
 * the launcher's context id).
 *
 * P1.3: three independent traces, written BEFORE anything that could
 * fault on an unexpected entry level, EL-agnostic:
 *   1. FB tick — 4 rows x 4 px white rect painted per core into the
 *      visible framebuffer; position derives from MPIDR only (never
 *      x0). Pure Device-memory stores; bypasses every cache / EL / x0
 *      theory: the tick proves the firmware really pointed a PC here.
 *      Layout: y=1496, x = 8 + slot*8, row pitch 2880 B, cell pitch
 *      32 B; slot = aff0 + 4*aff1 (this machine: 0..9).
 *   2. sec_probe[0..63] — mpidr-slot cells, 32 B each: x0 as
 *      delivered, CurrentEL, mpidr.
 *   3. sec_probe[64..127] — x0-slot cells, ONLY when x0 <= 15
 *      (bounded; a garbage context id must not corrupt memory).
 * Then fork: EL1 -> full setup + C park; any other EL -> WFE.
 */
	.globl secondary_entry
secondary_entry:
	msr	daifset, #0xf		/* first: no interrupts mid-record */
	mrs	x2, CurrentEL
	mrs	x3, mpidr_el1

	/* slot = aff0 + 4*aff1 (never uses x0: it may be garbage) */
	ubfx	x5, x3, #0, #2
	ubfx	x6, x3, #8, #2
	add	x5, x5, x6, lsl #2

	/* FB tick: base (1496*720 + 8)*4 = 0x41BE20 + slot*32 */
	mov	x4, #0xEC000000
	add	x4, x4, #0x41B, lsl #12
	add	x4, x4, #0xE20
	add	x4, x4, x5, lsl #5
	mov	x7, #0xffffffff
	str	x7, [x4]
	str	x7, [x4, #8]
	add	x4, x4, #0xB40
	str	x7, [x4]
	str	x7, [x4, #8]
	add	x4, x4, #0xB40
	str	x7, [x4]
	str	x7, [x4, #8]
	add	x4, x4, #0xB40
	str	x7, [x4]
	str	x7, [x4, #8]

	/* mpidr-slot record */
	adr	x1, sec_probe
	add	x1, x1, x5, lsl #5
	stp	x0, x2, [x1]
	str	x3, [x1, #16]

	/* x0-slot record, guarded: only when x0 <= 15 */
	cmp	x0, #15
	b.hi	1f
	adr	x1, sec_probe
	add	x1, x1, #0x800
	add	x1, x1, x0, lsl #5
	stp	x0, x2, [x1]
	str	x3, [x1, #16]
1:
	/* fork on entry level */
	cmp	x2, #0x4
	b.ne	2f
	msr	spsel, #1
	adr	x1, _sec_stacks
	add	x1, x1, x5, lsl #12
	add	x1, x1, #0x1000
	mov	sp, x1
	adr	x1, vectors
	msr	vbar_el1, x1
	isb
	bl	secondary_main
2:	wfe
	b	2b


/*
 * Exception vectors. Each slot stashes x29/x30, loads its slot number
 * and jumps to the common path, which saves x0-x28 and calls
 * exc_handler(slot, regs). IRQs are ignored; anything else paints blue
 * and halts (band 7 untouched: the summary stays dark).
 */
.macro vent slot
	.balign	0x80
	stp	x29, x30, [sp, #-16]!
	mov	x29, #\slot
	b	exc_common
.endm

	.balign	0x800
	.globl vectors
vectors:
	vent 0
	vent 1
	vent 2
	vent 3
	vent 4
	vent 5
	vent 6
	vent 7
	vent 8
	vent 9
	vent 10
	vent 11
	vent 12
	vent 13
	vent 14
	vent 15

exc_common:
	sub	sp, sp, #(30 * 8)
	stp	x0, x1, [sp, #0]
	stp	x2, x3, [sp, #16]
	stp	x4, x5, [sp, #32]
	stp	x6, x7, [sp, #48]
	stp	x8, x9, [sp, #64]
	stp	x10, x11, [sp, #80]
	stp	x12, x13, [sp, #96]
	stp	x14, x15, [sp, #112]
	stp	x16, x17, [sp, #128]
	stp	x18, x19, [sp, #144]
	stp	x20, x21, [sp, #160]
	stp	x22, x23, [sp, #176]
	stp	x24, x25, [sp, #192]
	stp	x26, x27, [sp, #208]
	str	x28, [sp, #224]
	mov	x0, x29
	mov	x1, sp
	bl	exc_handler
	ldp	x0, x1, [sp, #0]
	ldp	x2, x3, [sp, #16]
	ldp	x4, x5, [sp, #32]
	ldp	x6, x7, [sp, #48]
	ldp	x8, x9, [sp, #64]
	ldp	x10, x11, [sp, #80]
	ldp	x12, x13, [sp, #96]
	ldp	x14, x15, [sp, #112]
	ldp	x16, x17, [sp, #128]
	ldp	x18, x19, [sp, #144]
	ldp	x20, x21, [sp, #160]
	ldp	x22, x23, [sp, #176]
	ldp	x24, x25, [sp, #192]
	ldp	x26, x27, [sp, #208]
	ldr	x28, [sp, #224]
	add	sp, sp, #(30 * 8)
	ldp	x29, x30, [sp], #16
	eret
