/*
 * SBOOT-like collateral-damage entry for QEMU (qemu-sweet.py --sboot-state).
 *
 * Replicates the state SBOOT may leave behind when it jumps into the a30s
 * shim on the phone: MMU ON with flat 1:1 tables, D/I caches enabled, TLB
 * entries present for regions the caller already touched, junk in the
 * general registers. start.S still handles the EL drop itself; here we
 * force the dangerous remainder. If the shim->kernel chain dies only under
 * this state, the phone root cause is SBOOT's residue vs the shim's clean
 * assumptions ("written with the MMU and caches off"), and start.S must
 * harden the handoff (MMU off + TLBI/DSB before the jump).
 *
 * Everything is movz/movk immediates, so the harness needs no patching:
 *   MAIR: attr0 = 0xff (normal write-back)
 *   TCR:  T0SZ=32 (4 GiB VA), TG0=4K, IRGN0/ORGN0=2, SH0=3  -> 0x3a20
 *   TTBR0: 0x40201000 (the level-1 table below, 4K-aligned)
 *   DTB_PA 0x41000000, SHIM_PA 0x42000000 (qemu-sweet.py constants)
 */

.section .text
.globl _start

_start:
	/* Register residue, like a bootloader handing over mid-work. */
	movz	x1, #0x1111
	movz	x2, #0x2222
	movz	x3, #0x3333
	movz	x8, #0xdead
	movz	x9, #0xbeef

	/* MAIR_EL1: attr0 = 0xff normal write-back */
	movz	x17, #0xff
	msr	MAIR_EL1, x17

	/* TCR_EL1: T0SZ 32, 4K granule, WB/not-shareable-attrs */
	movz	x17, #0x3a20
	msr	TCR_EL1, x17

	/* TTBR0_EL1 = the level-1 table at 0x40201000 */
	movz	x17, #0x4020, lsl #16
	movk	x17, #0x1000
	msr	TTBR0_EL1, x17
	dsb	sy
	isb

	/* SCTLR_EL1 |= M|C|I -> MMU, D-cache, I-cache fully on */
	mrs	x16, sctlr_el1
	orr	x16, x16, #(1 << 0)
	orr	x16, x16, #(1 << 2)
	orr	x16, x16, #(1 << 12)
	msr	sctlr_el1, x16
	dsb	sy
	isb

	/* Dirty TLB: walk both regions the caller already touches. */
	ldr	x15, =0x41000000
	ldr	x15, [x15]		/* DTB page */
	ldr	x15, =0x42000000
	ldr	x15, [x15]		/* shim first words */
	ldr	x30, [x15]		/* LR = junk, like SBOOT's */

	/* Hand off SBOOT-style: x0 = FDT in hand, PC = shim entry. */
	ldr	x0, =0x41000000
	ldr	x16, =0x42000000
	br	x16

	.balign	0x1000

/* Level-1 table, 4 x 1GiB flat block entries: PA base | ATTR0 | AF | block. */
table:
	.quad	0x00000401
	.quad	0x40000401
	.quad	0x80000401
	.quad	0xc0000401
