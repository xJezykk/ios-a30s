#!/bin/bash
# Build a minimal HFS+ root volume for the A30s SWEET XNU bring-up.
#
#   mka30sroot.sh OUT.img [SIZE]
#
# The output is an image only. It is never written to a phone partition by
# this script. The image is intended for the eMMC root path:
#
#   rd=emmcroot0 emmcroot=userdata
#
# The host must provide a writable HFS+ mount implementation. WSL installations
# without loop-device support cannot populate HFS+ images and are rejected.
set -euo pipefail

[[ $# -ge 1 && $# -le 2 ]] || {
	echo "usage: $0 OUT.img [SIZE]" >&2
	exit 2
}

out=$(realpath -m "$1")
size=${2:-8M}
script_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
launcher=$(realpath "$script_dir/../xnu/out/diag-ramdisk-src/root/sbin/launchd")

[[ ! -e "$out" ]] || {
	echo "refusing to overwrite existing image: $out" >&2
	exit 1
}
command -v mkfs.hfsplus >/dev/null 2>&1 || {
	echo "missing mkfs.hfsplus" >&2
	exit 1
}
command -v mount >/dev/null 2>&1 || {
	echo "missing mount" >&2
	exit 1
}
command -v umount >/dev/null 2>&1 || {
	echo "missing umount" >&2
	exit 1
}
[[ -f "$launcher" ]] || {
	echo "missing diagnostic launcher: $launcher" >&2
	exit 1
}

work=$(mktemp -d)
mounted=0
cleanup() {
	if [[ $mounted == 1 ]]; then
		umount "$work/root" 2>/dev/null || true
	fi
	rmdir "$work/root" "$work" 2>/dev/null || true
}
trap cleanup EXIT

truncate -s "$size" "$out"
mkfs.hfsplus -s -v A30SUserdata "$out"
mkdir "$work/root"

if ! mount -t hfsplus -o loop,force "$out" "$work/root"; then
	echo "cannot mount HFS+ image; this host lacks writable loop/HFS+ support" >&2
	echo "use a Linux environment with hfsplus and loop devices, not this WSL mode" >&2
	exit 1
fi
mounted=1

install -d -m 0755 "$work/root/sbin" "$work/root/etc"
install -m 0755 "$launcher" "$work/root/sbin/launchd"
printf '%s\n' 'A30S HFS+ diagnostic root' > "$work/root/etc/README"
chmod 0644 "$work/root/etc/README"

sync
umount "$work/root"
mounted=0
fsck.hfsplus -f -n "$out"
echo "created verified HFS+ root: $out"
