#!/usr/bin/env python3
"""The identity element of the sandpile group on an n x n grid.

Here the grid has edges: grains that topple off the boundary are lost.
Stable piles that you can reach from *any* pile by adding grains (the
"recurrent" ones) form a group under "add cell by cell, then topple". Every
group has a zero, a pile e with stab(e + c) == c for every recurrent c.

You'd expect zero to look like nothing. It doesn't. With c_max = all 3s,
    e = stab(2*c_max - stab(2*c_max))
and the picture is anything but empty.
"""

import argparse
import time

from sandpile import write_png


def stabilize(h, side, interior):
    """Topple every interior cell until stable; the border is a sink."""
    unstable = [i for i in range(len(h)) if interior[i] and h[i] >= 4]
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
            if interior[j] and before < 4 <= before + q:
                unstable.append(j)
    for i in range(len(h)):
        if not interior[i]:
            h[i] = 0  # empty the sink
    return topples


def add(a, b, side, interior):
    c = [x + y for x, y in zip(a, b)]
    stabilize(c, side, interior)
    return c


def identity(n):
    side = n + 2
    interior = [0 < i // side < side - 1 and 0 < i % side < side - 1
                for i in range(side * side)]
    six = [6 if m else 0 for m in interior]
    s = six[:]
    topples = stabilize(s, side, interior)
    e = [x - y for x, y in zip(six, s)]
    topples += stabilize(e, side, interior)
    return e, side, interior, topples


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=128, help="grid side")
    ap.add_argument("--scale", type=int, default=4, help="pixels per cell")
    ap.add_argument("--out", default="sandpile_identity.png")
    ap.add_argument("--check", action="store_true",
                    help="verify e + e == e and e + c_max == c_max")
    args = ap.parse_args()

    t0 = time.perf_counter()
    e, side, interior, topples = identity(args.n)
    print(f"{args.n}x{args.n} identity: {topples} topplings "
          f"in {time.perf_counter() - t0:.1f} s")

    if args.check:
        c_max = [3 if m else 0 for m in interior]
        print("e + e     == e    :", add(e, e, side, interior) == e)
        print("e + c_max == c_max:", add(e, c_max, side, interior) == c_max)

    write_png(args.out, e, side, args.scale)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
