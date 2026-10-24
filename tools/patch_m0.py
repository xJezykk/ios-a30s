#!/usr/bin/env python3
"""M0+M1 boot-ladder instrument for the a30s SWEETXNU payload (phone round 2).

Round 2 (P2.6 on-device) showed: shim console runs through "XNU jumping",
then nothing. QEMU proves the same path boots to the AMFI plateau, so the
death is between the jump and the kernel's first console paint, on real
silicon. This patcher makes the payload answer two questions on the phone:

  M0  payload-entry override: a hand stub appended as an extra segment that
      paints a red strip at the panel bottom and branches to __start.
  M1  a patch over __start's opening branch + dead nop pad (13 instructions)
      that paints a magenta dot strip and re-emits the original branch with
      its exact PC-relative encoding.

Semantics:
- Both stubs preserve x0 (boot_args PA) and only clobber x2..x5/x16/x17,
  which _start_first_cpu re-initializes before any semantic use
  (verified: zero-fill loop, VBAR setup, boot_args loads all read fresh).
- M0 sits at kernel_end VA (a fresh page); kernel_end is bumped by one page
  so the shim places ADT/boot_args after it, exactly per loader.c.
- The seg table grows by one row: 32 bytes are INSERTED at the table's end
  (0x2a8 + 32*nsegs); every piece file_off at/after that point and adt_off
  shift +32; the new segment's bytes are appended at the payload tail.

Usage:
  patch_m0.py -i payload.bin -o out.bin
  patch_m0.py --no-m1 -i payload.bin -o out.bin   (bisect: M0 stub only)

Status (2026-10-24 round): validated end-to-end. The original build had
one bug that cost a full QEMU iteration: nsegs was packed at 0x08,
stamping `version` -> "bad payload header" -> PLFAIL -> probe screen
parked at band-1 smc. Fixed: nsegs @0x0C. With it, patched payloads
pass clean and sboot-hostile QEMU entry, and the on-device round
painted BOTH bars -> the phone death is inside the kernel head,
pre-console (root cause since identified: 16K-page TCR on v8.0; the
4K-page CI rebuild supersedes this instrument for the next round).

Limitations / TODO (next round):
- START (real __start VA) and FIRST_CPU (_start_first_cpu VA) are
  constants pinned to the current 16K CI kernel build; the 4K rebuild
  shifts both. Make the patcher derive them from the payload itself
  (Mach-O __TEXT_EXEC symbol table or relocations) instead of constants.
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile

PAGE = 0x1000
STEP = 32		# seg-table row size (4 Q fields)
ROW0 = 1536		# 24 rows above the panel bottom (720x1560x32)
ROWS = 8
COLS = 64		# words (=256 px wide strip)
STRIDE = 2880
FB = 0xEC000000
RED = 0xFFFF0000
GREEN = 0xFF00C700	# symmetric under R/B byte-swap: visible either way
START = 0xfffffe000748b488
FIRST_CPU = 0xfffffe000748d0ec
TEXT_EXEC_VA = 0xfffffe00072e4000
# __TEXT_EXEC piece row index is found by vmaddr; the piece holds __start.

HDR = struct.Struct('<8sII7Q')	# magic, ver, nsegs, ...cmdline at 0x48


def asm(src, org):
    """Assemble A64 source linked at org; return the binary bytes."""
    with tempfile.TemporaryDirectory() as d:
        s_path = os.path.join(d, 'stub.S')
        o_path = os.path.join(d, 'stub.o')
        e_path = os.path.join(d, 'stub.elf')
        b_path = os.path.join(d, 'stub.bin')
        open(s_path, 'w').write(src)
        subprocess.run(['clang', '--target=aarch64-unknown-none-elf',
                        '-c', s_path, '-o', o_path], check=True)
        subprocess.run(['ld.lld', '-N', '-Ttext', org, '-o', e_path, o_path],
                       check=True)
        subprocess.run(['llvm-objcopy', '-O', 'binary', e_path, b_path],
                       check=True)
        return open(b_path, 'rb').read()


def enc_b(src, dst):
    """Encode `b dst` from instruction address src (AArch64 PC-relative)."""
    disp = (dst - src) // 4
    assert -(1 << 26) <= disp < (1 << 26), f'b out of range: {disp:#x}'
    return 0x14000000 | (disp & 0x3FFFFFF)


def m0_source(strip0, bl_addr, bl_pc):
    src = f'''/* M0: entry paint, then branch to __start. x0 preserved. */
.text
.globl _start
_start:
	/* red strip: {ROWS} rows x {COLS} words at the panel bottom */
	movz	w17, #0x0000
	movk	w17, #0x{RED >> 16:04x}, lsl #16
	movz	x16, #0x{strip0 & 0xffff:04x}
	movk	x16, #0x{strip0 >> 16:04x}, lsl #16
	mov	x5, x16
	movz	x4, #0x{STRIDE:04x}
	movz	x3, #0x{ROWS:04x}
.Lr:
	movz	x2, #0x{COLS:04x}
.Li:
	str	w17, [x5], #4
	subs	x2, x2, #1
	b.ne	.Li
	add	x5, x5, x4
	subs	x3, x3, #1
	b.ne	.Lr
	/* raw b {bl_addr:#x}: disp = ({bl_addr:#x} - (m0+56))/4 */
	.word	0x{enc_b(bl_pc, bl_addr):x}
'''
    return src


def m1_source(strip1, bl_addr, bl_pc):
    src = f'''/* M1: 2x64 green strip at the __start pad, then the original branch. */
.text
.globl _start
_start:
	movz	x16, #0x{strip1 & 0xffff:04x}
	movk	x16, #0x{strip1 >> 16:04x}, lsl #16
	movz	w17, #0x{GREEN & 0xffff:04x}
	movk	w17, #0x{GREEN >> 16:04x}, lsl #16
	movz	x3, #0x2
.Lr:
	movz	x2, #0x{COLS:04x}
.Li:
	str	w17, [x16], #4
	subs	x2, x2, #1
	b.ne	.Li
	add	x16, x16, #0x{STRIDE - COLS * 4:04x}
	subs	x3, x3, #1
	b.ne	.Lr
	/* raw b {bl_addr:#x}: disp = ({bl_addr:#x} - (m1+48))/4 */
	.word	0x{enc_b(bl_pc, bl_addr):x}
'''
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-i', required=True, help='input payload')
    ap.add_argument('-o', required=True, help='output payload (patched)')
    ap.add_argument('--no-m1', action='store_true',
                    help='skip the M1 overwrite of __start (bisect helper)')
    a = ap.parse_args()

    buf = bytearray(open(a.i, 'rb').read())
    magic, ver, nsegs = HDR.unpack_from(buf, 0)[:3]
    assert magic == b'SWEETXNU' and ver == 1
    entry_va, adt_off, adt_size, kernel_end, phys_base, mem_size = \
        struct.unpack_from('<6Q', buf, 0x18)
    tab = 0x2a8
    tab_end = tab + STEP * nsegs
    print(f'nsegs={nsegs} entry={entry_va:#x} adt_off={adt_off:#x} '
          f'kernel_end={kernel_end:#x} size={len(buf):#x}')

    # places for the two markers
    m0_va = kernel_end
    strip0 = FB + ROW0 * STRIDE
    strip1 = strip0 + (ROWS + 2) * STRIDE	# two rows below the red strip
    m0_bin = asm(m0_source(strip0, START, m0_va + 56), hex(m0_va))
    m1_bin = asm(m1_source(strip1, FIRST_CPU, START + 48), hex(START))
    assert len(m0_bin) == 60 and len(m1_bin) == 52, (len(m0_bin), len(m1_bin))
    print(f'm0 bin={len(m0_bin)} B at VA {m0_va:#x} '
          f'painted strip at {strip0:#x}')
    print(f'm1 bin={len(m1_bin)} B at VA {START:#x} '
          f'painted dot at {strip1:#x}')

    # fileoff of the __start word in the payload: the piece containing VA.
    piece_i = None
    for i in range(nsegs):
        vmaddr, vmsize, off, size = struct.unpack_from('<4Q', buf, tab + STEP * i)
        if vmaddr <= START < vmaddr + vmsize:
            piece_i = i
            start_rel = START - vmaddr
    assert piece_i is not None, 'no piece holds __start?'
    # pieces are stored at payload index tab + 32 * i but with the mach-o
    # mapping off->va handled by mkpayload: the piece fileoff in the FILE is
    # looked up by the row; pieces are contiguous starting at tab_end.
    row = struct.unpack_from('<4Q', buf, tab + STEP * piece_i)
    piece_file_off = row[2]
    assert piece_file_off >= tab_end, 'piece impossibly early?'
    # payload layout: header + table (nsegs rows) + piece data in the SAME
    # order as the table rows. Pieces are contiguous, so piece 0's
    # fileoff == tab_end and each piece's fileoff is the true payload offset.
    start_file_off = row[2] + start_rel
    print(f'__start piece #{piece_i}: vmaddr={row[0]:#x} file_off={row[2]:#x} '
          f'size={row[3]:#x}; __start fileoff={start_file_off:#x}')

    # ---- surgery ----
    # 1) split: everything after the table tail moves by 32 (new row).
    pos = tab_end
    head_bytes_before_data = bytes(buf[:pos])
    tail = bytes(buf[pos:])
    new_row = struct.pack('<4Q', m0_va, PAGE, 0, len(m0_bin))
    buf2 = bytearray(head_bytes_before_data + new_row + tail)
    n_segs2 = nsegs + 1
    adt_off2 = adt_off + STEP

    # 2) fix every file_off at or after the (old) tab_end and adt_off; the
    #    data at payload position `pos` was shifted by 32 bytes.
    for i in range(n_segs2 - 1):
        r_off = tab + STEP * i
        vmaddr, vmsize, off, size = struct.unpack_from('<4Q', buf2, r_off)
        if off >= pos:
            struct.pack_into('<4Q', buf2, r_off, vmaddr, vmsize, off + STEP, size)
    struct.pack_into('<Q', buf2, 0x20, adt_off2)

    # 3) the new segment's file_off = after-ADT: everything currently ends
    #    at len(buf2); append stub bytes as the tail.
    m0_file_off = len(buf2)
    struct.pack_into('<4Q', buf2, tab + STEP * (n_segs2 - 1),
                     m0_va, PAGE, m0_file_off, len(m0_bin))
    buf2 += m0_bin
    # 4) header updates: nsegs +1, entry_va -> M0, adt_off +32, kernel_end
    struct.pack_into('<I', buf2, 0x0c, n_segs2)
    entry_va2 = m0_va
    kernel_end2 = m0_va + PAGE
    struct.pack_into('<Q', buf2, 0x18, entry_va2)
    struct.pack_into('<Q', buf2, 0x20, adt_off2)
    struct.pack_into('<Q', buf2, 0x30, kernel_end2)
    # virt_base @0x10, adt_size @0x28, phys_base @0x38, mem_size @0x40 and
    # the cmdline @0x48.. are all untouched.

    # 5) M1 patch over __start's words (they shifted with the inserted row);
    #    skippable for bisecting which payload change breaks the shim.
    if not a.no_m1:
        assert buf[start_file_off:start_file_off + 4] == b'\x19\x07\x00\x14', \
            'expected the b _start_first_cpu 0x14000719 little-endian at __start'
        m1_off = start_file_off + (STEP if start_file_off >= pos else 0)
        buf2[m1_off:m1_off + len(m1_bin)] = m1_bin
        print(f'M1 patched at fileoff {m1_off:#x} ({len(m1_bin)} B)')
    else:
        print('M1 skipped (--no-m1)')

    # sanity reads back
    assert struct.unpack_from('<Q', buf2, 0x18)[0] == entry_va2
    assert struct.unpack_from('<Q', buf2, 0x20)[0] == adt_off2
    print(f'nsegs={n_segs2} entry={entry_va2:#x} adt_off={adt_off2:#x} '
          f'kernel_end={kernel_end2:#x} size={len(buf2):#x}')

    open(a.o, 'wb').write(bytes(buf2))
    print(f'wrote {a.o}')


if __name__ == '__main__':
    main()
