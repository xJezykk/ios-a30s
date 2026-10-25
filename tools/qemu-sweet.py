#!/usr/bin/env python3
"""Boot a SWEETXNU payload (tools/mkpayload.py) in QEMU's virt board.

  qemu-sweet.py PAYLOAD OUTDIR [--secs N] [--gdb] [--live-serial] [--watch]
      [--qmp-stdio] [--a30s-fb]
  qemu-sweet.py --shim SHIM-Image-Payload OUTDIR [--watch] ...

--shim boots the REAL a30s shim instead of the stub: the shim binary
(shim/out/Image-payload) is loaded at SHIM_PA (the shim is
position-independent and its payload_info block is PC-relative, so any
free RAM works) and a QEMU-generated virt DTB rides at DTB_PA, handed
in x0 exactly like SBOOT hands its FDT. No RAM is pre-placed and no
stub jump into the kernel happens: the shim itself finds the SWEETXNU
payload it embeds, copies it and jumps. In this mode boot_args->Video
is the shim's own A30s panel constant (0xEC000000), so BOTH the early
painter and kprintf use the adtfb canvas — fb.png stays black.

Does what shim/loader.c does on the phone, but on the host: places the
kernel segments, ADT and boot_args at their physical addresses and starts
the CPU at a 6-instruction stub that jumps to the entry with x0 = boot_args.
virt RAM starts at 0x40000000; with 4G it covers the phone's framebuffer
(0x9c000000) and the kernel's phys_base (0xb0000000) as plain memory.

After N seconds (or when the guest stops) it writes into OUTDIR:
  fb.png     the 1080x2400 framebuffer (what the phone screen would show)
  regs.txt   QEMU's view of CPU state
  qemu.log   exceptions with ESR/FAR/ELR (-d int,guest_errors)

With --kernel KERNEL (the matching unstripped Mach-O), also decode the
framebuffer using its iso_font into fb.txt and write result.json. This is a
snapshot of the circular screen, not a complete boot log. --fail-on-panic
returns 2 when a panic is visible; absence of a panic does not prove boot.
An undecodable or blank screen returns 3 with --fail-on-panic.
For A30s tests, --a30s-fb also captures and decodes the 720x1560 ADT
framebuffer at 0xEC000000. --qmp-stdio uses QMP on standard I/O instead
of a local Unix socket.
"""
import argparse, hashlib, json, os, queue, shutil, socket, struct, subprocess, sys, threading, time, zlib

FB_BASE, FB_W, FB_H = 0x9c000000, 1080, 2400
FB_STRIDE = FB_W * 4
# Second console: the ADT "sweet-fbcon" the kernel's fbcon_probe() maps
# (mkadt-a30s.py FB constants; absolute phys, 720x1560, 2880 B stride).
ADT_FB, ADT_FB_W, ADT_FB_H, ADT_FB_STRIDE = 0xEC000000, 720, 1560, 2880
PAGE = 0x4000
STUB_PA = 0x40200000	# above the dtb QEMU places at RAM base
# --shim mode: SBOOT-like handoff, relocated to QEMU's RAM map. The
# phone loads the shim at 0x10008000, which is below virt RAM start,
# but the shim is position-independent (shim/build.sh drift fix).
SHIM_PA = 0x42000000	# shim image home (16.7 MB up to ~0x43100000)
DTB_PA = 0x41000000	# QEMU's generated FDT, handed to the shim in x0
BOOT_LINE_LENGTH = 608


def check_qemu_dependency(cpu='cortex-a76'):
    """Verify qemu-system-aarch64 binary and CPU model support."""
    qemu_bin = shutil.which('qemu-system-aarch64')
    if not qemu_bin:
        print('[!] Error: qemu-system-aarch64 not found in PATH.', file=sys.stderr)
        print('    Install instructions:', file=sys.stderr)
        print('      Arch Linux:    sudo pacman -S qemu-system-aarch64', file=sys.stderr)
        print('      Ubuntu/Debian: sudo apt install qemu-system-arm', file=sys.stderr)
        print('      Fedora:         sudo dnf install qemu-system-aarch64', file=sys.stderr)
        print('      macOS:          brew install qemu', file=sys.stderr)
        return False
    try:
        ver = subprocess.check_output([qemu_bin, '--version'], text=True).splitlines()[0]
        print(f'[*] Found QEMU: {ver}', file=sys.stderr)
    except Exception as e:
        print(f'[!] Warning: Failed to query QEMU version: {e}', file=sys.stderr)
        return False

    try:
        cpu_list = subprocess.check_output([qemu_bin, '-M', 'virt', '-cpu', 'help'], text=True)
        if cpu in cpu_list:
            print(f'[*] CPU model {cpu} supported', file=sys.stderr)
        else:
            print(f'[!] Warning: CPU model {cpu} not explicitly listed in qemu-system-aarch64 -cpu help', file=sys.stderr)
    except Exception:
        pass
    return True


def check_kernel_tools(kernel_path):
    """Verify llvm-nm is available when --kernel is specified."""
    if not shutil.which('llvm-nm'):
        print('[!] Error: llvm-nm not found in PATH, required for --kernel font extraction.', file=sys.stderr)
        print('    Install instructions:', file=sys.stderr)
        print('      Arch Linux:    sudo pacman -S llvm', file=sys.stderr)
        print('      Ubuntu/Debian: sudo apt install llvm', file=sys.stderr)
        print('      Fedora:         sudo dnf install llvm', file=sys.stderr)
        print('      macOS:          brew install llvm', file=sys.stderr)
        return False
    if not os.path.isfile(kernel_path):
        print(f'[!] Error: kernel file does not exist: {kernel_path}', file=sys.stderr)
        return False
    return True


def build_image(payload):
    """Return (phys_base, ram_image, boot_args_pa, entry_pa)."""
    magic, ver, nsegs = struct.unpack_from('<8sII', payload, 0)
    assert magic == b'SWEETXNU' and ver == 1, 'not a SWEETXNU v1 payload'
    virt_base, entry_va, adt_off, adt_size, kernel_end, phys_base, mem_size = \
        struct.unpack_from('<7Q', payload, 0x10)
    cmdline = payload[0x48:0x48 + BOOT_LINE_LENGTH]
    delta = phys_base - virt_base
    adt_pa = kernel_end + delta
    ba_pa = (adt_pa + adt_size + PAGE - 1) & ~(PAGE - 1)
    top = ba_pa + PAGE
    img = bytearray(top - phys_base)
    for i in range(nsegs):
        vmaddr, vmsize, foff, fsize = struct.unpack_from('<4Q', payload, 0x2a8 + 32 * i)
        o = vmaddr + delta - phys_base
        img[o:o + fsize] = payload[foff:foff + fsize]
    img[adt_pa - phys_base:adt_pa - phys_base + adt_size] = payload[adt_off:adt_off + adt_size]
    ba = struct.pack('<HH4x4Q6QI4xQI', 2, 2, virt_base, phys_base, mem_size, top,
                     FB_BASE, 1, FB_STRIDE, FB_W, FB_H, 32, 0, adt_pa - delta, adt_size)
    ba += cmdline + b'\0' * 4 + struct.pack('<QQ', 0, mem_size)
    assert len(ba) == 736
    img[ba_pa - phys_base:ba_pa - phys_base + len(ba)] = ba
    return phys_base, bytes(img), ba_pa, entry_va + delta


def stub(ba_pa, entry_pa):
    # ldr x0, =ba ; ldr x1, =entry ; br x1 ; nop ; .quad ba ; .quad entry
    return struct.pack('<4I2Q', 0x58000080, 0x580000a1, 0xd61f0020, 0xd503201f, ba_pa, entry_pa)


def dump_virt_dtb(path, cpu, ram_gib, gicv2):
    """QEMU's own generated FDT for the virt board (handed to the shim in x0)."""
    subprocess.run(['qemu-system-aarch64',
                    '-M', f'virt,gic-version={2 if gicv2 else 3}',
                    '-cpu', cpu, '-m', f'{ram_gib}G', '-display', 'none',
                    '-machine', f'dumpdtb={path}'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_embedded_payload(image):
    """Parse the shim's A30P payload_info block (see shim/build.sh)."""
    pinfo = image.find(b'A30P\x01\x00\x00\x00')
    if pinfo < 0:
        sys.exit('--shim: no A30P payload_info block found (image built without a payload?)')
    delta, plsize = struct.unpack_from('<QQ', image, pinfo + 8)
    blob = image[pinfo + delta: pinfo + delta + plsize]
    assert blob[:8] == b'SWEETXNU', 'embedded blob is not a SWEETXNU payload'
    return blob


def assemble_sboot_entry(bin_path):
    """Assemble tools/sboot_entry.S (SBOOT-like hostile entry) to a raw blob.

    The blob is position-fixed: placed at STUB_PA, its hand-written constants
    expect the level-1 table at STUB_PA + 0x1000, the DTB at DTB_PA and the
    shim at SHIM_PA.
    """
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sboot_entry.S')
    obj = bin_path[:-4] + '.o'
    subprocess.run(['clang', '--target=aarch64-unknown-none-elf', '-c', src, '-o', obj], check=True)
    subprocess.run(['llvm-objcopy', '-O', 'binary', obj, bin_path], check=True)
    os.unlink(obj)
    blob = os.path.getsize(bin_path)
    assert blob >= 0x1020, f'sboot entry blob too small ({blob:#x})'
    return bin_path


def write_png(path, raw, w=FB_W, h=FB_H, stride=FB_STRIDE):
    rows = []
    for y in range(h):
        line = raw[y * stride:(y + 1) * stride]
        rgb = bytearray(w * 3)	# a8r8g8b8 little endian = B G R A in memory
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        rows.append(b'\0' + bytes(rgb))
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


def payload_data(payload, va, size):
    """Read initialized segment bytes by VA (the payload has no slide)."""
    nsegs = struct.unpack_from('<I', payload, 12)[0]
    for i in range(nsegs):
        vmaddr, _, foff, fsize = struct.unpack_from('<4Q', payload, 0x2a8 + 32 * i)
        if vmaddr <= va and va + size <= vmaddr + fsize:
            offset = foff + va - vmaddr
            return payload[offset:offset + size]
    raise ValueError(f'VA {va:#x}+{size:#x} not in initialized payload segments')


def decode_screen(raw, font, w=FB_W, h=FB_H, stride=FB_STRIDE):
    """Exact glyph matching for pe_serial.c's 2x, white-on-black fbcon."""
    if w < 16 or h < 32 or stride < w * 4:
        raise ValueError(f'invalid framebuffer geometry: {w}x{h}, stride={stride}')
    if len(raw) < stride * h:
        raise ValueError(f'framebuffer data truncated: {len(raw)} < {stride * h} bytes')
    if len(font) < 127 * 16:
        raise ValueError(f'font data truncated: {len(font)} < {127 * 16} bytes')
    glyphs = {font[c * 16:(c + 1) * 16]: chr(c) for c in range(32, 127)}
    rows, unknown = [], 0
    for row in range(h // 32):
        chars = []
        for col in range(w // 16):
            glyph = bytearray(16)
            for y in range(16):
                offset = (row * 32 + y * 2) * stride + col * 16 * 4
                for x in range(8):
                    if raw[offset + x * 8:offset + x * 8 + 4] == b'\xff' * 4:
                        glyph[y] |= 1 << x
            key = bytes(glyph)
            char = glyphs.get(key, '?')
            unknown += key not in glyphs
            chars.append(char)
        rows.append(''.join(chars).rstrip())
    return '\n'.join(rows).rstrip() + '\n', unknown


def screen_result(screen, unknown):
    if 'panic(cpu ' in screen or 'SWEET EARLY PANIC:' in screen:
        status = 'panic'
    elif unknown:
        status = 'incomplete_decode'
    elif screen.strip():
        status = 'no_panic_observed'
    else:
        status = 'blank_screen'
    return {'status': status, 'unknown_glyphs': unknown, 'snapshot_only': True,
            'observed_stage': 'framebuffer_snapshot', 'full_boot_verified': False}


def apply_expected_text(result, screen, expected):
    """Record console markers without upgrading a snapshot to a full boot."""
    result['expected_text'] = expected
    result['missing_expected_text'] = [item for item in expected if item not in screen]
    result['markers_satisfied'] = not result['missing_expected_text']
    result['full_boot_verified'] = False
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class QMPStdio:
    """Small synchronous QMP client for environments that forbid local sockets."""
    def __init__(self, process):
        self.process = process
        self.messages = queue.Queue()
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        while True:
            message = self.messages.get(timeout=15)
            if 'QMP' in message:
                break
        response = self.call('qmp_capabilities')
        if 'error' in response:
            raise RuntimeError(f'QMP command qmp_capabilities failed: {response["error"]}')

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue

    def call(self, execute, **arguments):
        call_id = self.next_id
        self.next_id += 1
        self.process.stdin.write(json.dumps({
            'execute': execute, 'arguments': arguments, 'id': call_id,
        }) + '\n')
        self.process.stdin.flush()
        while True:
            message = self.messages.get(timeout=30)
            if message.get('id') == call_id:
                return message


def qmp(sock_path, *cmds, pmemsave=None):
    """Run QEMU human-monitor commands over QMP (synchronous, unlike the echoing HMP socket)."""
    if isinstance(sock_path, QMPStdio):
        call = sock_path.call
    else:
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock_path)
        f = s.makefile('rw')
        f.readline()

        def call(execute, **args):
            f.write(json.dumps({'execute': execute, 'arguments': args}) + '\n')
            f.flush()
            while True:
                r = json.loads(f.readline())
                if 'return' in r or 'error' in r:
                    return r

        response = call('qmp_capabilities')
        if 'error' in response:
            s.close()
            raise RuntimeError(f'QMP command qmp_capabilities failed: {response["error"]}')
    out = []
    for c in cmds:
        r = call('human-monitor-command', **{'command-line': c})
        if 'error' in r:
            print(f'qmp {c!r}: {r["error"]}', file=sys.stderr)
            raise RuntimeError(f'QMP command {c!r} failed: {r["error"]}')
        out.append(r.get('return', ''))
    if pmemsave:	# native QMP: HMP's argument parser chokes on '-' in paths
        r = call('pmemsave', val=pmemsave[0], size=pmemsave[1], filename=pmemsave[2])
        if 'error' in r:
            print(f'qmp pmemsave: {r["error"]}', file=sys.stderr)
            raise RuntimeError(f'QMP pmemsave failed: {r["error"]}')
    if not isinstance(sock_path, QMPStdio):
        s.close()
    return out


def stream_serial_file(serial_path, file_pos, out_stream=sys.stdout):
    """Read newly appended content from serial.txt and write directly to stream."""
    if not os.path.exists(serial_path):
        return file_pos
    try:
        with open(serial_path, 'r', errors='replace') as f:
            f.seek(file_pos)
            data = f.read()
            if data:
                out_stream.write(data)
                out_stream.flush()
                return f.tell()
    except Exception:
        pass
    return file_pos


def main():
    ap = argparse.ArgumentParser(
        description="Boot a SWEETXNU payload in QEMU's virt board.",
        epilog="Examples:\n"
               "  qemu-sweet.py payload.bin out/\n"
               "  qemu-sweet.py payload.bin out/ --live-serial\n"
               "  qemu-sweet.py payload.bin out/ --kernel kernel.macho --fail-on-panic\n"
               "  qemu-sweet.py --check-env\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('payload', nargs='?', help='SWEETXNU payload binary (from mkpayload.py)')
    ap.add_argument('outdir', nargs='?', help='Output directory for fb.png, regs.txt, and logs')
    ap.add_argument('--secs', type=float, default=20, help='Duration to run QEMU in seconds (default: 20)')
    ap.add_argument('--gdb', action='store_true', help='start halted, gdb stub on :1234, no timeout')
    ap.add_argument('--hmp', action='append', default=[], help='extra monitor command run after stop (repeatable), e.g. "x /8gx 0xfffffe..."')
    ap.add_argument('--cpu', default='cortex-a76', help='QEMU CPU model (default: cortex-a76)')
    ap.add_argument('--gicv2', action='store_true',
                    help='virt board with gic-version=2 (A30s GIC-400 kernel: 0082 compiles out GICv3)')
    ap.add_argument('--qmp-stdio', action='store_true',
                    help='use QMP over stdio instead of a local Unix socket')
    ap.add_argument('--shim', metavar='IMAGE-PAYLOAD', help='boot the REAL a30s shim (shim/out/Image-payload) instead of the stub: SBOOT-style handoff with x0 = a QEMU-generated DTB; the single positional is then OUTDIR')
    ap.add_argument('--sboot-state', action='store_true',
                    help='with --shim: enter the shim with MMU+caches on, dirty TLB and junked regs (SBOOT leftover state; tools/sboot_entry.S)')
    ap.add_argument('--kernel', help='matching unstripped kernel, for exact framebuffer text decoding (uses llvm-nm)')
    ap.add_argument('--fail-on-panic', action='store_true', help='exit 2 for a visible panic, 3 for incomplete/blank decoding; requires --kernel')
    ap.add_argument('--expect-text', action='append', default=[],
                    help='require this exact decoded console substring (repeatable; does not assert full boot)')
    ap.add_argument('--fail-on-missing-text', action='store_true',
                    help='exit 4 when any --expect-text marker is absent')
    ap.add_argument('--live-serial', action='store_true', help='stream serial console output to stdout in real time')
    ap.add_argument('--fb-dump', metavar='ADDR,SIZE', help='extra pmemsave region at stop, e.g. 0xEC000000,0x1400000 (the ADT sweet-fbcon the kernel fbcon maps)')
    ap.add_argument('--a30s-fb', action='store_true',
                    help='capture/decode A30s ADT fbcon at 0xEC000000 (720x1560)')
    ap.add_argument('--watch', action='store_true', help='keep fb.png/adtfb.png refreshed in the out dir while the VM runs (open in an auto-reloading viewer, e.g. gwenview)')
    ap.add_argument('--watch-int', type=float, default=1.0, help='watch refresh interval in seconds (default: 1.0)')
    ap.add_argument('--check-env', action='store_true', help='check QEMU installation and dependencies without running a payload')
    a = ap.parse_args()

    if a.check_env:
        ok = check_qemu_dependency(a.cpu)
        if shutil.which('llvm-nm'):
            print('[*] llvm-nm found in PATH', file=sys.stderr)
        else:
            print('[!] Optional: llvm-nm not found (needed only when using --kernel for text decoding)', file=sys.stderr)
        return 0 if ok else 1

    if a.shim:
        # with --shim the first positional carries OUTDIR instead of a payload
        if a.payload and not a.outdir:
            a.outdir, a.payload = a.payload, None
        if not a.outdir:
            ap.error('outdir (positional) is required with --shim')
    elif not a.payload or not a.outdir:
        ap.error('payload and outdir are required unless --check-env is specified')

    if a.sboot_state and not a.shim:
        ap.error('--sboot-state requires --shim')

    if a.fail_on_panic and not a.kernel:
        ap.error('--fail-on-panic requires --kernel')
    if a.a30s_fb and not a.kernel:
        ap.error('--a30s-fb requires --kernel for font-based console decoding')
    if a.fail_on_missing_text and not a.expect_text:
        ap.error('--fail-on-missing-text requires at least one --expect-text')
    if a.expect_text and not a.kernel:
        ap.error('--expect-text requires --kernel')

    if not check_qemu_dependency(a.cpu):
        return 1

    if a.kernel and not check_kernel_tools(a.kernel):
        return 1

    shim_image = None
    input_hashes = {}
    if a.shim:
        shim_image = open(a.shim, 'rb').read()
        payload = find_embedded_payload(shim_image)
        input_hashes[os.path.abspath(a.shim)] = sha256_file(a.shim)
    else:
        payload = open(a.payload, 'rb').read()
        input_hashes[os.path.abspath(a.payload)] = sha256_file(a.payload)
    if a.kernel:
        input_hashes[os.path.abspath(a.kernel)] = sha256_file(a.kernel)
    font = None
    if a.kernel:
        symbols = subprocess.check_output(['llvm-nm', '--defined-only', a.kernel], text=True)
        fonts = [int(line.split()[0], 16) for line in symbols.splitlines()
                 if line.split()[-1:] == ['_iso_font']]
        if len(fonts) != 1:
            ap.error('kernel must contain exactly one _iso_font symbol')
        font = payload_data(payload, fonts[0], 256 * 16)
    os.makedirs(a.outdir, exist_ok=True)
    out = os.path.abspath(a.outdir)

    # payload header: virt_base@0x10 ... phys_base@0x38, mem_size@0x40
    phys_base = struct.unpack_from('<Q', payload, 0x38)[0]
    mem_size = struct.unpack_from('<Q', payload, 0x40)[0]
    ram_gib = max(4, -(-(phys_base + mem_size - 0x40000000) // (1 << 30)))

    mon = f'{out}/mon.sock'
    if not a.qmp_stdio and os.path.exists(mon):
        os.unlink(mon)
    cmd = ['qemu-system-aarch64', '-M', f'virt,gic-version={2 if a.gicv2 else 3}', '-accel', 'tcg',
           '-cpu', a.cpu, '-smp', '1', '-m', f'{ram_gib}G',
           '-serial', f'file:{out}/serial.txt',
           '-d', 'int,guest_errors,unimp', '-D', f'{out}/qemu.log', '-no-reboot']
    if a.qmp_stdio:
        cmd += ['-display', 'none', '-monitor', 'none', '-qmp', 'stdio']
    else:
        cmd += ['-nographic', '-qmp', f'unix:{mon},server,nowait']
    if a.shim:
        open(f'{out}/shim-image.bin', 'wb').write(shim_image)
        open(f'{out}/payload-embedded.bin', 'wb').write(payload)
        if a.sboot_state:
            # SBOOT-like hostile entry: the assembled blob turns the MMU and
            # caches on with flat tables, dirties the TLB and jumps to the
            # shim itself. Position-fixed to STUB_PA (table at STUB_PA+0x1000).
            entry_bin = assemble_sboot_entry(f'{out}/sboot-entry.bin')
            print('sboot-state: shim entered with MMU+caches on, dirty TLB (tools/sboot_entry.S)', file=sys.stderr)
        else:
            open(f'{out}/stub.bin', 'wb').write(stub(DTB_PA, SHIM_PA))  # x0 = FDT; branch = shim entry
            entry_bin = f'{out}/stub.bin'
        dtb = f'{out}/virt.dtb'
        if not os.path.exists(dtb):
            dump_virt_dtb(dtb, a.cpu, ram_gib, a.gicv2)
        print(f'shim={a.shim} @{SHIM_PA:#x} x0=dtb@{DTB_PA:#x} payload={len(payload):#x} phys={phys_base:#x}', file=sys.stderr)
        cmd += ['-device', f'loader,file={out}/shim-image.bin,addr={SHIM_PA:#x},force-raw=on',
                '-device', f'loader,file={dtb},addr={DTB_PA:#x},force-raw=on',
                '-device', f'loader,file={entry_bin},addr={STUB_PA:#x},force-raw=on',
                '-device', f'loader,addr={STUB_PA:#x},cpu-num=0']
    else:
        _, img, ba_pa, entry_pa = build_image(payload)
        open(f'{out}/ram.bin', 'wb').write(img)
        open(f'{out}/stub.bin', 'wb').write(stub(ba_pa, entry_pa))
        print(f'phys_base={phys_base:#x} boot_args={ba_pa:#x} entry={entry_pa:#x}', file=sys.stderr)
        cmd += ['-device', f'loader,file={out}/ram.bin,addr={phys_base:#x},force-raw=on',
                '-device', f'loader,file={out}/stub.bin,addr={STUB_PA:#x},force-raw=on',
                '-device', f'loader,addr={STUB_PA:#x},cpu-num=0']
    if a.gdb:
        cmd += ['-S', '-gdb', 'tcp::1234']

    p = None
    serial_file = f'{out}/serial.txt'
    serial_pos = 0

    try:
        if a.qmp_stdio:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True, bufsize=1)
            mon = QMPStdio(p)
        else:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        if a.gdb:
            print('gdb stub on :1234; QMP socket:', mon, file=sys.stderr)
            p.wait()
            return 0

        t0 = time.time()
        last_refresh = 0.0
        last_log_check = 0.0
        spiral_halted = False
        qlog = f'{out}/qemu.log'
        while p.poll() is None and time.time() - t0 < a.secs:
            if a.live_serial:
                serial_pos = stream_serial_file(serial_file, serial_pos)
            now = time.time()
            # A guest exception storm logs GB/s into qemu.log (the qsboot
            # run filled the whole tmpfs once). Freeze the VM at the onset:
            # processes stop issuing, the stop-state artifacts stay intact.
            if not spiral_halted and now - last_log_check >= 1.0:
                last_log_check = now
                try:
                    if os.path.getsize(qlog) > 32 << 20:
                        qmp(mon, 'stop')
                        spiral_halted = True
                        print('[!] exception storm detected: guest halted at the onset', file=sys.stderr)
                except Exception:
                    pass
            if a.watch and now - last_refresh >= a.watch_int:
                try:
                    qmp(mon, pmemsave=(FB_BASE, FB_STRIDE * FB_H, f'{out}/fb.raw'))
                    write_png(f'{out}/fb.png', open(f'{out}/fb.raw', 'rb').read())
                    qmp(mon, pmemsave=(ADT_FB, ADT_FB_STRIDE * ADT_FB_H, f'{out}/adtfb.raw'))
                    write_png(f'{out}/adtfb.png', open(f'{out}/adtfb.raw', 'rb').read(),
                              ADT_FB_W, ADT_FB_H, ADT_FB_STRIDE)
                    last_refresh = time.time()
                except Exception:
                    pass
            time.sleep(0.2)

        if a.live_serial:
            stream_serial_file(serial_file, serial_pos)

        result = {'status': 'qemu_exited', 'qemu_returncode': p.returncode,
                  'full_boot_verified': False}
        execution = {'qemu_returncode_before_harness_stop': p.returncode,
                     'stopped_by_harness': False, 'guest_was_paused': False,
                     'requested_seconds': a.secs, 'elapsed_seconds': round(time.time() - t0, 3)}
        if p.poll() is None:
            dumps = []
            if a.fb_dump:
                addr_s, size_s = a.fb_dump.split(',', 1)
                name = 'fb-dump.raw' if a.a30s_fb else 'adtfb.raw'
                dumps.append((int(addr_s, 0), int(size_s, 0), f'{out}/{name}'))
            if a.a30s_fb:
                dumps.append((ADT_FB, ADT_FB_STRIDE * ADT_FB_H, f'{out}/adtfb.raw'))
            _, guest_status, regs, *extra = qmp(mon, 'stop', 'info status', 'info registers',
                                  *a.hmp,
                                  pmemsave=(FB_BASE, FB_STRIDE * FB_H, f'{out}/fb.raw'))
            execution['guest_status_at_snapshot'] = guest_status.strip()
            execution['guest_was_paused'] = True
            for addr, size, fname in dumps:	# VM is stopped but alive here
                qmp(mon, pmemsave=(addr, size, fname))
            open(f'{out}/regs.txt', 'w').write(regs + ''.join(f'\n(qemu) {c}\n{r}' for c, r in zip(a.hmp, extra)))
            p.kill()
            p.wait()
            execution['stopped_by_harness'] = True
            execution['harness_stop_signal'] = 'SIGKILL after snapshot; not a guest shutdown'
            raw = open(f'{out}/fb.raw', 'rb').read()
            write_png(f'{out}/fb.png', raw)
            result = {'status': 'not_decoded'}
            if font is not None:
                screen, unknown = decode_screen(raw, font)
                open(f'{out}/fb.txt', 'w').write(screen)
                result = screen_result(screen, unknown)
                apply_expected_text(result, screen, a.expect_text)
            if a.a30s_fb:
                adt_raw = open(f'{out}/adtfb.raw', 'rb').read()
                write_png(f'{out}/adtfb.png', adt_raw,
                          ADT_FB_W, ADT_FB_H, ADT_FB_STRIDE)
                screen, unknown = decode_screen(adt_raw, font,
                                                ADT_FB_W, ADT_FB_H,
                                                ADT_FB_STRIDE)
                open(f'{out}/adtfb.txt', 'w').write(screen)
                result = screen_result(screen, unknown)
                apply_expected_text(result, screen, a.expect_text)
                result['console_source'] = 'A30s ADT framebuffer 0xEC000000 (720x1560)'
            os.unlink(f'{out}/fb.raw')
        else:
            print(f'qemu exited early: {p.returncode}', file=sys.stderr)

        open(f'{out}/result.json', 'w').write(json.dumps(result, indent=2) + '\n')
        outputs = ['result.json', 'regs.txt', 'qemu.log', 'serial.txt', 'fb.png', 'fb.txt']
        if a.a30s_fb:
            outputs += ['adtfb.png', 'adtfb.txt']
        artifact_hashes = {name: sha256_file(f'{out}/{name}')
                           for name in outputs if os.path.isfile(f'{out}/{name}')}
        harness_exitcode = 0
        if a.fail_on_panic and result['status'] == 'panic':
            harness_exitcode = 2
        elif a.fail_on_panic and result['status'] in ('incomplete_decode', 'blank_screen'):
            harness_exitcode = 3
        elif a.fail_on_missing_text and result.get('missing_expected_text'):
            harness_exitcode = 4
        elif result['status'] == 'qemu_exited':
            harness_exitcode = 1
        try:
            qemu_version = subprocess.check_output(
                ['qemu-system-aarch64', '--version'], text=True).splitlines()[0]
        except Exception:
            qemu_version = 'unavailable'
        metadata = {
            'mode': 'shim_sboot_state' if a.shim and a.sboot_state else
                    ('shim' if a.shim else 'direct_entry'),
            'simulated': True, 'branch_head': subprocess.run(
                ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() or None,
            'qemu_version': qemu_version, 'qemu_command': cmd,
            'machine': f'virt,gic-version={2 if a.gicv2 else 3}',
            'accelerator': 'tcg', 'cpu': a.cpu, 'input_sha256': input_hashes,
            'execution': execution, 'result': result,
            'harness_exitcode': harness_exitcode,
            'artifact_sha256': artifact_hashes,
        }
        open(f'{out}/run-metadata.json', 'w').write(json.dumps(metadata, indent=2) + '\n')
        print(json.dumps(result), file=sys.stderr)
        extra_outputs = f'  {out}/adtfb.png  {out}/adtfb.txt' if a.a30s_fb else ''
        print(f'{out}/fb.png  {out}/regs.txt  {out}/qemu.log{extra_outputs}', file=sys.stderr)
        return harness_exitcode

    except KeyboardInterrupt:
        print('\n[!] Interrupted by user, stopping QEMU...', file=sys.stderr)
        if p and p.poll() is None:
            p.kill()
            p.wait()
        return 130
    finally:
        ram_file = f'{out}/ram.bin'
        if os.path.exists(ram_file):
            try:
                os.unlink(ram_file)
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
