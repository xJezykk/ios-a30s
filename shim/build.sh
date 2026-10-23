#!/bin/bash
# Build the a30s shim and pack it as a Samsung-style boot image SBOOT
# accepts (Android boot header v1, 2048-byte pages, load at 0x10008000).
#   P1 probe (default):   ./build.sh                    -> out/shim-a30s-p1.img
#   XNU payload build:    ./build.sh PAYLOAD [OUT]      -> out/shim-a30s-p2.img
# The payload (mkpayload.py output) is appended to a copy of out/Image
# behind the linker-script info block _payload_info; loader.c finds it
# PC-relatively, so SBOOT's drifting load base (P1.5c) never matters.
set -euo pipefail
cd "$(dirname "$0")"

CC="clang --target=aarch64-unknown-none-elf"
CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra"

PAYLOAD="${1:-}"
OUTIMG="${2:-out/shim-a30s-p2.img}"
KIMG="out/Image"

mkdir -p out
for f in start.S a30s.c console.c loader.c; do
	$CC $CFLAGS -c $f -o out/${f%.*}.o
done

abs=$(llvm-readelf -r out/a30s.o out/console.o out/loader.o 2>/dev/null | grep -c ABS64 || true)
[ "$abs" = 0 ] || { echo "error: $abs absolute relocations in C objects" >&2; exit 1; }

ld.lld -T shim.ld -N -o out/shim.elf \
	out/start.o out/a30s.o out/console.o out/loader.o

llvm-objcopy -O binary out/shim.elf "$KIMG"

end=$(llvm-nm out/shim.elf | awk '$3=="_end"{print $1}')
sz=$(( 0x$end ))
truncate -s "$sz" "$KIMG"

pinfo=$(llvm-nm out/shim.elf | awk '$3=="_payload_info"{print $1}')
[ -n "$pinfo" ] || { echo "error: no _payload_info symbol" >&2; exit 1; }

if [ -n "$PAYLOAD" ]; then
	KIMG="out/Image-payload"
	python3 - out/Image "$KIMG" "$((0x$pinfo))" "$PAYLOAD" <<'EOF'
import struct, sys
image, out_name, pinfo_off, payload_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
img = bytearray(open(image, 'rb').read())
pl = open(payload_path, 'rb').read()
img += b'\x00' * (-len(img) % 16)
start = len(img)                       # payload file offset == runtime delta from Image start
img += pl
delta = start - pinfo_off              # same delta at runtime: binary maps vaddr 1:1
struct.pack_into('<IIQQ', img, pinfo_off, 0x50303341, 1, delta, len(pl))  # "A30P"
open(out_name, 'wb').write(img)
print(f"patch: pinfo@{pinfo_off:#x} payload@{start:#x} size={len(pl):#x} delta={delta:#x}")
EOF
else
	OUTIMG="out/shim-a30s-p1.img"
fi

# Empty newc ramdisk (SBOOT likely still wants one; a few dozen bytes).
if command -v cpio >/dev/null; then
	(cd out && rm -rf rd && mkdir rd && cd rd && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
else
	python3 - <<'EOF'
import os
# minimal newc-style ramdisk: empty file list
data = b'070701'
data += b'0' * (8 * 13)   # inode, mode, uid, gid, nlink, mtime, filesize, major, minor
data += (11).to_bytes(8, 'big')          # namesize
data += b'0' * 8                          # chksum
data += b'TRAILER!!!' + b'\0' * 5
data = data.ljust(512, b'\0')
open('out/ramdisk.bin', 'wb').write(data)
EOF
fi

mkbootimg --header_version 1 --kernel "$KIMG" --ramdisk out/ramdisk.bin \
	--base 0x10000000 --kernel_offset 0x8000 --ramdisk_offset 0x10008000 \
	--tags_offset 0x10000100 --pagesize 2048 \
	--cmdline "console=ram" -o "$OUTIMG"

ls -l "$KIMG" "$OUTIMG"
python3 - "$OUTIMG" "$KIMG" <<'EOF'
import struct, sys
kern = open(sys.argv[2], 'rb').read()
d = open(sys.argv[1], 'rb').read()
assert d[:8] == b'ANDROID!'
k, ka = struct.unpack('<II', d[8:16])
assert k == len(kern), (k, len(kern))
print("pack: kernel_size=%#x kernel_addr=%#x page=%#x (kernel %d bytes ok)" % (
    k, ka, struct.unpack('<I', d[36:40])[0], k))
EOF
