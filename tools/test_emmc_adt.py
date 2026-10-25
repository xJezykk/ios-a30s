#!/usr/bin/env python3
"""Regression check for the A30s DW-MMC description in the generated ADT."""
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mkadt_a30s', ROOT / 'tools/mkadt-a30s.py')
mkadt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mkadt)
sys.path.insert(0, str(ROOT / 'tools'))
import adt  # noqa: E402

root = mkadt.build()
node = next(child for child in root.children if child.name == 'sweet-emmc')
props = {name: value for name, _flags, value in node.props}
assert props['compatible'] == b'samsung,exynos-dw-mshc\0'
assert props['reg'] == mkadt.u64(0x13500000, 0x2000)
assert props['bus-width'] == mkadt.u32(8)
assert props['non-removable'] == b''
serialized = adt.serialize(root)
parsed, end = adt.parse(serialized)
assert end == len(serialized)
parsed_node = next(child for child in parsed.children if child.name == 'sweet-emmc')
assert parsed_node.prop('reg') == props['reg']

# The first-stage probe must remain a strictly read-only snapshot until the
# controller handoff has been measured on the target.
patch = (ROOT / 'xnu/patches/0086-a30s-emmc-readonly-probe.patch').read_text()
source = patch.split('+++ b/bsd/dev/sweet_emmc.c\n', 1)[1]
source = re.sub(r'^@@[^\n]*\n', '', source, count=1)
source = '\n'.join(line[1:] for line in source.splitlines() if line.startswith('+'))
assert 'PE_parse_boot_argn("emmcprobe"' in source
assert 'emmc_read(base,' in source
assert 'emmc_write' not in source
assert 'EMMC_CMDARG' not in source and 'EMMC_DATA' not in source
assert not re.search(r'\*\(volatile uint32_t \*\).*=', source)
print('A30s eMMC ADT: PASS (physical base, extent, bus width, non-removable)')
print('A30s eMMC probe: PASS (opt-in, read-only register snapshot)')
