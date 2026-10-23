/*
 * Text console for the a30s shim. Derived from ios-sweet/shim/console.c
 * (MIT), constants adapted to the Samsung A30s panel: 720x1560, SCALE=2,
 * text zone starts below the result bands.
 */
#include "console.h"
#include "font.h"

#define SCALE		2
#define CH_W		(8 * SCALE)
#define CH_H		(16 * SCALE)
#define MARGIN		24
#define LABEL_COLS	8

volatile uint32_t *con_fb = (volatile uint32_t *)FB_BASE;
static uint32_t con_bg;
static uint32_t con_y = 0;	/* first text row's top edge */
static int row;

static void fill_rect(uint32_t y0, uint32_t h, uint32_t color)
{
	for (uint32_t yy = 0; yy < h; yy++) {
		volatile uint32_t *r = con_fb + (y0 + yy) * (FB_STRIDE / 4);
		for (int x = 0; x < FB_W; x++)
			r[x] = color;
	}
}

void band_reset(void)
{
	for (int n = 0; n < NBANDS; n++)
		fill_rect(BAND_OF_Y(n), BAND_H, DARK);
}

void band_set(int n, uint32_t color)
{
	fill_rect(BAND_OF_Y(n), BAND_H, color);
}

void con_ytop(uint32_t y)
{
	con_y = y;
	row = 0;
}

void con_clear(uint32_t bg)
{
	int y;
	for (y = 0; y < FB_H; y++) {
		volatile uint32_t *line = con_fb + y * (FB_STRIDE / 4);
		for (int x = 0; x < FB_W; x++)
			line[x] = bg;
	}
	con_bg = bg;
	row = 0;
}

static void put_char(int col, char ch, uint32_t fg)
{
	if (ch < 0x20 || ch > 0x7e)
		ch = '?';
	const unsigned char *g = font8x16[ch - 0x20];
	int x0 = MARGIN + col * CH_W, y0 = con_y + MARGIN + row * CH_H;
	if (x0 + CH_W > FB_W || y0 + CH_H > FB_H)
		return;
	for (int gy = 0; gy < 16; gy++)
		for (int gx = 0; gx < 8; gx++) {
			uint32_t c = (g[gy] & (0x80 >> gx)) ? fg : con_bg;
			for (int sy = 0; sy < SCALE; sy++) {
				volatile uint32_t *p = con_fb + (y0 + gy * SCALE + sy) * (FB_STRIDE / 4)
						       + x0 + gx * SCALE;
				for (int sx = 0; sx < SCALE; sx++)
					p[sx] = c;
			}
		}
}

static int put_str(int col, const char *s, uint32_t fg)
{
	while (*s)
		put_char(col++, *s++, fg);
	return col;
}

static void fmt_hex(char *buf, uint64_t v, int digits)
{
	static const char hex[] = "0123456789ABCDEF";
	for (int i = 0; i < digits; i++)
		buf[i] = hex[(v >> (4 * (digits - 1 - i))) & 0xf];
	buf[digits] = 0;
}

void con_skip(void)
{
	row++;
}

void con_str(const char *label, const char *s)
{
	put_str(0, label, YELLOW);
	put_str(LABEL_COLS, s, WHITE);
	row++;
}

void con_hex(const char *label, uint64_t v)
{
	char buf[17];
	fmt_hex(buf, v, 16);
	con_str(label, buf);
}

void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb)
{
	char buf[9];
	int col = put_str(0, label, YELLOW);
	col = col < LABEL_COLS ? LABEL_COLS : col + 1;
	col = put_str(col, a, YELLOW);
	fmt_hex(buf, va, 8);
	col = put_str(col, buf, WHITE);
	if (b) {
		col = put_str(col + 1, b, YELLOW);
		fmt_hex(buf, vb, 8);
		put_str(col, buf, WHITE);
	}
	row++;
}

void con_hex3(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb,
	      const char *c, uint32_t vc)
{
	char buf[9];
	int col = put_str(0, label, YELLOW);
	col = col < LABEL_COLS ? LABEL_COLS : col + 1;
	col = put_str(col, a, YELLOW);
	fmt_hex(buf, va, 8);
	col = put_str(col, buf, WHITE);
	col = put_str(col + 1, b, YELLOW);
	fmt_hex(buf, vb, 8);
	col = put_str(col, buf, WHITE);
	col = put_str(col + 1, c, YELLOW);
	fmt_hex(buf, vc, 8);
	put_str(col, buf, WHITE);
	row++;
}

void con_mark(uint32_t y, uint32_t x, uint32_t w, uint32_t h, uint32_t color)
{
	uint32_t yy;
	for (yy = 0; yy < h; yy++) {
		volatile uint32_t *r = con_fb + (y + yy) * (FB_STRIDE / 4) + x;
		uint32_t xx;
		for (xx = 0; xx < w; xx++)
			r[xx] = color;
	}
}

void con_cursor(unsigned r)
{
	row = r;
}
