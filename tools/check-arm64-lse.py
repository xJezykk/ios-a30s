#!/usr/bin/env python3
"""Fail if ARM64 Mach-O kernel function ranges contain LSE instructions.

The scanner walks function ranges in executable instruction sections, while
excluding Mach-O data-in-code records and compiler jump tables identified by
their local symbols. This covers blocks reached through indirect jump tables,
which a simple control-flow walk can miss. Mixed/unmarked executable sections
and sections without function metadata fail closed. Undecodable words are
reported for review; Capstone 5 decodes the ARMv8.1 LSE instruction families
checked below.
"""

import argparse
import re
import struct
import sys

try:
    from capstone import CS_ARCH_ARM64, CS_MODE_ARM, Cs
except ImportError:
    raise SystemExit("capstone is required (CI pins capstone==5.0.6)")

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_SEGMENT_64 = 0x19
LC_FUNCTION_STARTS = 0x26
LC_SYMTAB = 0x2
LC_DATA_IN_CODE = 0x29
VM_PROT_EXECUTE = 0x4
S_ATTR_PURE_INSTRUCTIONS = 0x80000000
S_ATTR_SOME_INSTRUCTIONS = 0x00000400

LSE_MNEMONIC = re.compile(
    r"^(?:ldadd|ldclr|ldeor|ldset|ldsmax|ldsmin|ldumax|ldumin|"
    r"stadd|stclr|steor|stset|stsmax|stsmin|stumax|stumin|cas|casp|swp)"
    r"(?:b|h)?(?:a|l|al)?$"
)
def read_uleb(data, offset, end):
    value = shift = 0
    while offset < end:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("invalid ULEB128 in LC_FUNCTION_STARTS")
    raise ValueError("truncated ULEB128 in LC_FUNCTION_STARTS")


def parse_macho(data):
    if len(data) < 32:
        raise ValueError("file is shorter than a 64-bit Mach-O header")
    magic, cputype, _subtype, filetype, ncmds, sizeofcmds, _flags, _reserved = struct.unpack_from(
        "<IiiIIIII", data, 0
    )
    if magic != MH_MAGIC_64:
        raise ValueError("input is not a little-endian 64-bit Mach-O")
    if cputype != CPU_TYPE_ARM64:
        raise ValueError("input is not an ARM64 Mach-O")
    if filetype not in (2, 6):  # MH_EXECUTE or MH_DYLINKER
        raise ValueError("input is not an executable Mach-O")

    cursor = 32
    commands_end = cursor + sizeofcmds
    if commands_end > len(data):
        raise ValueError("load commands extend beyond end of file")
    sections = []
    text_base = None
    starts_info = None
    symtab = None
    data_in_code_info = None
    section_ordinal = 0
    for _ in range(ncmds):
        if cursor + 8 > commands_end:
            raise ValueError("truncated Mach-O load command")
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > commands_end:
            raise ValueError("invalid Mach-O load command size")
        if cmd == LC_SYMTAB:
            if cmdsize < 24:
                raise ValueError("truncated LC_SYMTAB")
            symtab = struct.unpack_from("<4I", data, cursor + 8)
        elif cmd == LC_DATA_IN_CODE:
            if cmdsize < 16:
                raise ValueError("truncated LC_DATA_IN_CODE")
            data_in_code_info = struct.unpack_from("<II", data, cursor + 8)
        elif cmd == LC_FUNCTION_STARTS:
            if cmdsize < 16:
                raise ValueError("truncated LC_FUNCTION_STARTS")
            starts_info = struct.unpack_from("<II", data, cursor + 8)
        elif cmd == LC_SEGMENT_64:
            if cmdsize < 72:
                raise ValueError("truncated LC_SEGMENT_64")
            segname = data[cursor + 8 : cursor + 24].split(b"\0", 1)[0].decode("ascii", "replace")
            vmaddr, _vmsize, _fileoff, _filesize = struct.unpack_from("<QQQQ", data, cursor + 24)
            initprot = struct.unpack_from("<i", data, cursor + 60)[0]
            nsects = struct.unpack_from("<I", data, cursor + 64)[0]
            section_cursor = cursor + 72
            if section_cursor + nsects * 80 > cursor + cmdsize:
                raise ValueError("section table exceeds LC_SEGMENT_64")
            if segname == "__TEXT":
                text_base = vmaddr
            for _ in range(nsects):
                sect = data[section_cursor : section_cursor + 80]
                section_ordinal += 1
                sectname = sect[:16].split(b"\0", 1)[0].decode("ascii", "replace")
                section_segname = sect[16:32].split(b"\0", 1)[0].decode("ascii", "replace")
                addr, size = struct.unpack_from("<QQ", sect, 32)
                offset = struct.unpack_from("<I", sect, 48)[0]
                flags = struct.unpack_from("<I", sect, 64)[0]
                if initprot & VM_PROT_EXECUTE and size:
                    sections.append((section_segname, sectname, addr, size, offset, flags, section_ordinal))
                section_cursor += 80
        cursor += cmdsize

    if not sections:
        raise ValueError("Mach-O has no file-backed executable sections")
    if text_base is None or starts_info is None:
        raise ValueError("Mach-O lacks __TEXT or LC_FUNCTION_STARTS metadata")
    dataoff, datasize = starts_info
    if dataoff + datasize > len(data):
        raise ValueError("LC_FUNCTION_STARTS extends beyond end of file")
    starts = []
    offset, end, cumulative = dataoff, dataoff + datasize, 0
    while offset < end:
        delta, offset = read_uleb(data, offset, end)
        if delta == 0:
            break
        cumulative += delta
        starts.append(text_base + cumulative)
    if not starts:
        raise ValueError("LC_FUNCTION_STARTS contains no function entries")
    jump_tables = {}
    if symtab:
        symoff, nsyms, stroff, strsize = symtab
        if symoff + nsyms * 16 > len(data) or stroff + strsize > len(data):
            raise ValueError("symbol table extends beyond end of file")
        by_section = {}
        for i in range(nsyms):
            strx, n_type, n_sect, _desc, value = struct.unpack_from("<IBBHQ", data, symoff + i * 16)
            if not strx or strx >= strsize or not n_sect or (n_type & 0x0E) != 0x0E:
                continue
            name_end = data.find(b"\0", stroff + strx, stroff + strsize)
            if name_end >= 0:
                name = data[stroff + strx : name_end].decode("utf-8", "replace")
                by_section.setdefault(n_sect, []).append((value, name))
        for section in sections:
            ordinal = section[6]
            symbols = sorted(by_section.get(ordinal, []))
            for i, (start, symbol) in enumerate(symbols):
                if not symbol.startswith("lJTI"):
                    continue
                end = next((value for value, _ in symbols[i + 1 :] if value > start), None)
                if end is not None:
                    jump_tables.setdefault(ordinal, []).append((start, end))
    data_ranges = []
    if data_in_code_info:
        dataoff, datasize = data_in_code_info
        if dataoff + datasize > len(data) or datasize % 8:
            raise ValueError("invalid LC_DATA_IN_CODE table")
        for offset in range(dataoff, dataoff + datasize, 8):
            fileoff, length, _kind = struct.unpack_from("<IHH", data, offset)
            data_ranges.append((fileoff, fileoff + length))
    return sections, starts, jump_tables, data_ranges


def scan(path):
    with open(path, "rb") as kernel_file:
        data = kernel_file.read()
    sections, starts, jump_tables, data_ranges = parse_macho(data)
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    md.detail = True
    confirmed, ambiguous, undecoded, unmarked, unmapped = [], [], [], [], []
    scanned_words = decoded_words = skipped_data_words = 0
    for section in sections:
        segname, sectname, addr, size, offset, flags, ordinal = section
        name = f"{segname},{sectname}"
        pure = bool(flags & S_ATTR_PURE_INSTRUCTIONS)
        some = bool(flags & S_ATTR_SOME_INSTRUCTIONS)
        if not pure and not some:
            unmarked.append(name)
            continue
        if offset + size > len(data):
            raise ValueError(f"{name} section extends beyond end of file")
        section_starts = [start for start in starts if addr <= start < addr + size]
        if not section_starts:
            unmapped.append(name)
            continue
        for index, start in enumerate(section_starts):
            end = section_starts[index + 1] if index + 1 < len(section_starts) else addr + size
            table_ranges = jump_tables.get(ordinal, [])
            for pc in range(start, end & ~3, 4):
                pos = offset + pc - addr
                if any(a <= pc < b for a, b in table_ranges) or any(a <= pos < b for a, b in data_ranges):
                    skipped_data_words += 1
                    continue
                scanned_words += 1
                decoded = next(md.disasm(data[pos : pos + 4], pc), None)
                if decoded is None or decoded.size != 4:
                    undecoded.append((name, pc, data[pos : pos + 4].hex()))
                    continue
                decoded_words += 1
                if LSE_MNEMONIC.match(decoded.mnemonic):
                    (confirmed if pure else ambiguous).append((name, pc, decoded.mnemonic, decoded.op_str))

    print(f"Mach-O executable sections: {len(sections)}; instruction words examined: {scanned_words}")
    print(f"Decoded words: {decoded_words}; annotated data words skipped: {skipped_data_words}")
    print(f"Pure-instruction section LSE matches: {len(confirmed)}")
    print(f"Mixed-instruction section LSE candidates: {len(ambiguous)}")
    print(f"Unmarked executable sections: {len(unmarked)}; sections without function starts: {len(unmapped)}")
    print(f"Undecodable words in function ranges: {len(undecoded)}")
    for section, address, mnemonic, operands in confirmed[:25]:
        print(f"LSE: {section} 0x{address:x}: {mnemonic} {operands}")
    if len(confirmed) > 25:
        print(f"... {len(confirmed) - 25} additional confirmed matches omitted")
    for section, address, mnemonic, operands in ambiguous[:25]:
        print(f"AMBIGUOUS: {section} 0x{address:x}: {mnemonic} {operands}")
    for section, address, word in undecoded[:10]:
        print(f"UNDECODED: {section} 0x{address:x}: {word}")
    if undecoded:
        print(f"... {max(0, len(undecoded) - 10)} additional undecodable words omitted")
    if unmarked:
        print("Unmarked executable sections (not instruction-scanned): " + ", ".join(unmarked))
    if unmapped:
        print("Executable sections without function starts: " + ", ".join(unmapped))
    if confirmed or ambiguous or unmarked or unmapped:
        return 1
    print("PASS: no decoded LSE operations in executable function ranges")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel", help="ARM64 Mach-O kernel executable")
    args = parser.parse_args()
    try:
        return scan(args.kernel)
    except (OSError, ValueError, struct.error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
