#pragma once

/*
 * a30s.shim freestanding base types - replaces <stdint.h> because the
 * build runs with no host headers and no libc at all.
 */

typedef unsigned char uint8_t, u8;
typedef unsigned short uint16_t, u16;
typedef unsigned int uint32_t, u32;
typedef unsigned long long uint64_t, u64;
typedef long long int64_t;
typedef char int8_t;
typedef short int16_t;
typedef int int32_t;

typedef unsigned long uintptr_t;

#define NR_CPUS		8

#define NULL ((void *)0)
