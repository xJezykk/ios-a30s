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
arm_io = next(child for child in root.children if child.name == 'arm-io')
node = next(child for child in arm_io.children if child.name == 'dwmmc0')
assert not any(child.name == 'sweet-emmc' for child in root.children)
props = {name: value for name, _flags, value in node.props}
assert props['compatible'] == b'samsung,exynos-dw-mshc\0'
assert props['reg'] == mkadt.u64(0x12500000, 0x2000)
assert props['cmdq-reg'] == mkadt.u64(0x12508000, 0x200)
assert props['interrupt'] == mkadt.u32(178)
assert props['bus-width'] == mkadt.u32(8)
assert props['non-removable'] == mkadt.u32(1)
serialized = adt.serialize(root)
parsed, end = adt.parse(serialized)
assert end == len(serialized)
parsed_arm_io = next(child for child in parsed.children if child.name == 'arm-io')
parsed_node = next(child for child in parsed_arm_io.children if child.name == 'dwmmc0')
assert parsed_node.prop('reg') == props['reg']

# The legacy probe remains a strictly read-only snapshot, independent of the
# separate opt-in block driver.
probe = (ROOT / 'xnu/patches/0086-a30s-emmc-readonly-probe.patch').read_text()
assert 'PE_parse_boot_argn("emmcprobe"' in probe
assert 'emmc_read(base,' in probe
assert 'emmc_write' not in probe
assert 'EMMC_CMDARG' not in probe and 'EMMC_DATA' not in probe
assert not re.search(r'\*\(volatile uint32_t \*\).*=', probe)

driver = (ROOT / 'xnu/patches/0088-a30s-dwmmc-emmc.patch').read_text()
assert 'PE_parse_boot_argn("emmcw"' in driver
assert 'se.writable = (rw == 1)' in driver
assert 'lba - se.first_sector' in driver
assert 'emmcroot0' in driver
assert 'emmc_cmd(0, 0, CMD_INIT, NULL)' in driver
print('A30s eMMC ADT: PASS (SOC-relative windows, bus width, non-removable)')
print('A30s eMMC probe: PASS (opt-in, read-only register snapshot)')
print('A30s eMMC block device: PASS (writes opt-in, partition-bounded)')
