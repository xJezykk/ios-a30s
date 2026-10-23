#pragma once
#include "a30s.h"

/*
 * Text console on the framebuffer Samsung SBOOT leaves running
 * (s3cfb.bootloaderfb=0xEC000000, panel S6E8FC1 720x1560 HD+ ARGB).
 * The top of the screen is reserved for the pass/fail result bands:
 * they are painted before any text so a garbled/failed console still
 * communicates. con_ytop() moves the text zone below the bands.
 */

#define FB_BASE		0xEC000000UL
#define FB_W		720
#define FB_H		1560
#define FB_STRIDE	(FB_W * 4)	/* assumed; verified by P1 boot */

#define BAND_OF_Y(n)	((n) * 36)
#define BAND_H		30

#define RED	0xffa00000U
#define GREEN	0xff00a000U
#define BLUE	0xff0000a0U
#define WHITE	0xffffffffU
#define YELLOW	0xffffff00U
#define BLACK	0xff000000U
#define AMBER	0xffc08000U
#define DARK	0xff202020U

/* number of result bands; light with band_set() */
#define NBANDS	8

extern volatile uint32_t *con_fb;

void band_reset(void);
/* light band n: GREEN=pass, RED=fail, AMBER=info, BLUE=skipped */
void band_set(int n, uint32_t color);

void con_ytop(uint32_t y);
void con_clear(uint32_t bg);
void con_skip(void);
void con_str(const char *label, const char *s);
void con_hex(const char *label, uint64_t v);
/* label, then "a=xxxxxxxx b=xxxxxxxx" style pairs of 32-bit values */
void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb);
/* like con_hex2 but with three value fields on one line */
void con_hex3(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb,
	      const char *c, uint32_t vc);
/* progress marker cell (w x h px) at an arbitrary fb position */
void con_mark(uint32_t y, uint32_t x, uint32_t w, uint32_t h, uint32_t color);
/* park the text row cursor at an absolute row (panic handler) */
void con_cursor(unsigned r);
