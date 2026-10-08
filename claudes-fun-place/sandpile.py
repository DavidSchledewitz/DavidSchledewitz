#!/usr/bin/env python3
"""Abelian sandpile: pour N grains on one cell and watch a fractal fall out.

Rule: any cell holding 4 or more grains topples, sending one grain to each
of its four neighbours. Repeat until every cell holds 0-3 grains. The final
pattern doesn't depend on the order of topplings (that's the "Abelian" part),
which lets us topple a cell in bulk: h grains -> h % 4 stay, h // 4 to each
neighbour.

Writes a PNG using only the standard library (zlib + struct).
"""

import argparse
import math
import struct
import time
import zlib

# One colour per final height 0..3. Night sky, deep violet, ember, starlight.
PALETTE = [(12, 14, 33), (74, 46, 120), (214, 92, 58), (247, 220, 140)]


def grid_side(grains):
    radius = math.sqrt(grains / (2.125 * math.pi))  # mean height is ~2.125
    return 2 * int(radius) + 21  # a little dark sky around the edge


def topple(grains):
    side = grid_side(grains)
    centre = side // 2
    h = [0] * (side * side)
    start = centre * side + centre
    h[start] = grains
    topples = stabilize(h, side, start)
    return h, side, topples


def stabilize(h, side, start):
    """Topple from cell `start` until the whole grid is stable."""
    unstable = [start]
    topples = 0
    while unstable:
        i = unstable.pop()
        q = h[i] >> 2
        if not q:
            continue
        h[i] &= 3
        topples += q
        for j in (i - 1, i + 1, i - side, i + side):
            before = h[j]
            h[j] = before + q
            if before < 4 <= before + q:
                unstable.append(j)
    return topples


def write_png(path, h, side, scale):
    rows = []
    for y in range(side):
        line = bytearray()
        for x in range(side):
            line += bytes(PALETTE[h[y * side + x]]) * scale
        rows.append(b"\x00" + bytes(line))
    raw = b"".join(row for row in rows for _ in range(scale))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    size = side * scale
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grains", type=int, default=2**16)
    ap.add_argument("--scale", type=int, default=3, help="pixels per cell")
    ap.add_argument("--out", default="sandpile.png")
    args = ap.parse_args()

    t0 = time.perf_counter()
    h, side, topples = topple(args.grains)
    dt = time.perf_counter() - t0
    write_png(args.out, h, side, args.scale)

    counts = [h.count(k) for k in range(4)]
    print(f"{args.grains} grains -> {topples} topplings in {dt:.1f} s")
    print(f"grid {side}x{side}, cells at height 0/1/2/3: {counts}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
