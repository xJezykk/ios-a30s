#include <assert.h>
#include <stdint.h>

#include "../../shim/fdt.h"

int main(void)
{
	static const uint8_t names[] = {
		0, 0, 0, 0,                 /* empty root name */
		'c', 'h', 'o', 's', 'e', 'n', 0, 0,
		'c', 'p', 'u', '@', '1', '0', '0', 0,
	};
	uintptr_t base = (uintptr_t)names;
	uintptr_t p = fdt_skip_node_name(base, base + sizeof(names));

	assert(p == base + 4);
	p = fdt_skip_node_name(p, base + sizeof(names));
	assert(p == base + 12);
	p = fdt_skip_node_name(p, base + sizeof(names));
	assert(p == base + sizeof(names));
	assert(fdt_skip_node_name(base + sizeof(names), base + sizeof(names)) == 0);
	return 0;
}
