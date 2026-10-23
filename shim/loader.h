#pragma once
#include "a30s.h"

/*
 * P2.2: boot an XNU payload (mkpayload.py output), A30s edition.
 * Returns 0 on success (xnu never returns: we jumped). On failure
 * returns a static diagnostic string and the P1 probe continues.
 */
const char *loader_boot(uint64_t dtb);
