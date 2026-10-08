#!/usr/bin/env python3
"""Toy cosmic-ray muon rain on a stack of square detector planes.

Sea-level muon flux through a horizontal surface is roughly 1 cm^-2 min^-1,
with an angular distribution ~cos^2(theta) per solid angle. Counting through a
horizontal plane adds one more cos(theta), so cos(theta) is drawn from a cos^3
pdf, i.e. cos(theta) = U**(1/4).
"""

import argparse
import math
import random

FLUX_PER_CM2_S = 1.0 / 60.0


def simulate(seconds, planes, size_cm, gap_cm, rng):
    depth = gap_cm * (planes - 1)
    margin = 3.0 * depth  # covers tracks up to ~72 deg that clip lower planes
    gen_side = size_cm + 2 * margin
    expected = FLUX_PER_CM2_S * gen_side**2 * seconds
    n_muons = poisson(expected, rng)

    hits_per_plane = [0] * planes
    multiplicity = [0] * (planes + 1)
    events = []
    for _ in range(n_muons):
        x0 = rng.uniform(-gen_side / 2, gen_side / 2)
        y0 = rng.uniform(-gen_side / 2, gen_side / 2)
        cos_t = rng.random() ** 0.25
        tan_t = math.sqrt(1 - cos_t**2) / cos_t
        phi = rng.uniform(0, 2 * math.pi)
        dx, dy = tan_t * math.cos(phi), tan_t * math.sin(phi)

        fired = []
        for i in range(planes):
            z = i * gap_cm
            x, y = x0 + dx * z, y0 + dy * z
            if abs(x) <= size_cm / 2 and abs(y) <= size_cm / 2:
                fired.append(i)
        if not fired:
            continue
        for i in fired:
            hits_per_plane[i] += 1
        multiplicity[len(fired)] += 1
        events.append((math.degrees(math.acos(cos_t)), fired))
    return hits_per_plane, multiplicity, events


def poisson(mean, rng):
    if mean > 50:  # normal approximation is plenty for a toy
        return max(0, round(rng.gauss(mean, math.sqrt(mean))))
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def draw(event, planes):
    theta, fired = event
    cells = ["*" if i in fired else "." for i in range(planes)]
    return f"  theta={theta:5.1f} deg  " + " ".join(cells)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--planes", type=int, default=4)
    ap.add_argument("--size", type=float, default=10.0, help="plane side, cm")
    ap.add_argument("--gap", type=float, default=2.0, help="plane spacing, cm")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    hits, mult, events = simulate(args.seconds, args.planes, args.size, args.gap, rng)

    print(f"Muon rain: {args.seconds:g} s on {args.planes} planes "
          f"of {args.size:g}x{args.size:g} cm, {args.gap:g} cm apart\n")
    print("Hits per plane (top to bottom):")
    for i, h in enumerate(hits):
        print(f"  plane {i}: {h:6d}  {'#' * min(h // max(1, max(hits) // 40), 40)}")
    print("\nPlanes fired per muon:")
    for k in range(1, args.planes + 1):
        print(f"  {k}: {mult[k]}")
    full = mult[args.planes]
    print(f"\nFull {args.planes}-fold coincidences: {full} "
          f"({full / args.seconds:.2f} Hz)")
    print("\nA few muons ('*' = plane fired):")
    for ev in events[:8]:
        print(draw(ev, args.planes))


if __name__ == "__main__":
    main()
