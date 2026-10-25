#pragma once

/* The caller supplies an FDT structure-block end. */
static inline uintptr_t fdt_skip_node_name(uintptr_t p, uintptr_t end)
{
	while (p < end && *(const volatile uint8_t *)p != 0)
		p++;
	if (p == end)
		return 0;
	p = (p + 4) & ~(uintptr_t)3;
	return p <= end ? p : 0;
}
