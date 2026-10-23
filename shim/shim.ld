ENTRY(_head)
SECTIONS
{
	. = 0;
	.text : { *(.text.head) *(.text*) }
	.rodata : { *(.rodata*) }
	.data : { *(.data*) }
	. = ALIGN(16);
	_bss_start = .;
	.bss : { *(.bss*) *(COMMON) }
	. = ALIGN(16);
	_bss_end = .;
	. += 0x8000;
	. = ALIGN(16);
	_stack_top = .;
	. = ALIGN(4096);
	_sec_stacks = .;
	. += 16 * 0x1000;	/* mpidr-slot indexing reaches slot 15 */
	. = ALIGN(16);
	/* embedded-payload info block; build.sh patches it after the
	 * payload is appended (magic/flags/off/size). Unpatched (probe
	 * builds) has magic 0xffffffff -> loader skips the channel. */
	_payload_info = .;
	.payload_info : {
		payload_info = .;
		LONG(0xffffffff)
		LONG(0xffffffff)
		QUAD(0)
		QUAD(0)
	}
	. = ALIGN(16);
	_end = .;
	/DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
