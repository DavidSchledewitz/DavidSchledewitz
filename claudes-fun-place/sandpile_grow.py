#!/usr/bin/env python3
"""Animate a sandpile growing, written out as a looping GIF.

Because the sandpile is Abelian, pouring grains in batches and stabilising
after each batch ends in exactly the same pile as pouring them all at once.
So each frame is: add a batch to the centre, topple, snapshot.

The GIF (including its LZW compressor) is written with the standard library.
"""

import argparse
import struct
import time

from sandpile import PALETTE, grid_side, stabilize


def lzw(pixels, min_size):
    """GIF-flavoured LZW: variable-width codes, packed LSB first."""
    clear, eoi = 1 << min_size, (1 << min_size) + 1
    out = bytearray()
    acc = nbits = 0

    def emit(code):
        nonlocal acc, nbits
        acc |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8

    def reset():
        nonlocal table, next_code, size
        table, next_code, size = {}, eoi + 1, min_size + 1

    table = next_code = size = None
    reset()
    emit(clear)
    prefix = pixels[0]
    for k in pixels[1:]:
        key = (prefix << 8) | k
        code = table.get(key)
        if code is not None:
            prefix = code
            continue
        emit(prefix)
        if next_code < 4096:
            table[key] = next_code
            next_code += 1
            # The decoder learns each code one step later, so widen one late.
            if next_code - 1 == 1 << size and size < 12:
                size += 1
        else:
            emit(clear)
            reset()
        prefix = k
    emit(prefix)
    emit(eoi)
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out)


def frame_pixels(h, side, scale):
    rows = []
    for y in range(side):
        row = bytes(v for v in h[y * side:(y + 1) * side] for _ in range(scale))
        rows.extend([row] * scale)
    return b"".join(rows)


def write_gif(path, frames, size, delays):
    gif = bytearray(b"GIF89a")
    gif += struct.pack("<HHBBB", size, size, 0xF1, 0, 0)  # 4-colour global table
    for rgb in PALETTE:
        gif += bytes(rgb)
    gif += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"  # loop forever
    for pixels, delay in zip(frames, delays):
        gif += b"\x21\xF9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
        gif += b"\x2C" + struct.pack("<HHHHB", 0, 0, size, size, 0)
        data = lzw(pixels, 2)
        gif.append(2)
        for i in range(0, len(data), 255):
            block = data[i:i + 255]
            gif.append(len(block))
            gif += block
        gif.append(0)
    gif.append(0x3B)
    with open(path, "wb") as f:
        f.write(gif)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grains", type=int, default=2**16)
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--scale", type=int, default=2, help="pixels per cell")
    ap.add_argument("--out", default="sandpile_grow.gif")
    args = ap.parse_args()

    side = grid_side(args.grains)
    centre = (side // 2) * side + side // 2
    h = [0] * (side * side)

    t0 = time.perf_counter()
    frames, poured, topples = [], 0, 0
    for k in range(1, args.frames + 1):
        # Quadratic schedule: area ~ grains, so the radius grows steadily.
        target = round(args.grains * (k / args.frames) ** 2)
        h[centre] += target - poured
        poured = target
        topples += stabilize(h, side, centre)
        frames.append(frame_pixels(h, side, args.scale))

    delays = [6] * (len(frames) - 1) + [300]  # hundredths of a second
    write_gif(args.out, frames, side * args.scale, delays)
    print(f"{poured} grains, {topples} topplings, {len(frames)} frames "
          f"in {time.perf_counter() - t0:.1f} s -> {args.out}")


if __name__ == "__main__":
    main()
