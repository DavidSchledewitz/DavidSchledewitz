# Claude's Fun Place

A little corner of Awesome_Cosmics where Claude gets to play.

## What's here

### `muon_rain.py`

A toy cosmic-ray muon shower for your terminal. Muons rain down at the sea-level
rate (~1 per cm² per minute, with a cos²θ zenith-angle distribution) onto a
stack of detector planes, and the script prints which planes fired and how
many events made a full coincidence.

```
python3 muon_rain.py              # default: 4 planes, 60 s
python3 muon_rain.py --seconds 600 --planes 3 --seed 42
```

No dependencies beyond the Python standard library.

## Rules of the fun place

1. Things here should be fun.
2. Things here should still be physically roughly right.
3. If rule 1 and rule 2 disagree, rule 2 wins, but rule 1 gets a footnote.
