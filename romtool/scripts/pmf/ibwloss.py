"""Scratch patch: drywall loss_db = -1 means use the iBwave Drywall (Plaster) table, log-f interpolation."""
import math
from romtool import autoplace as A, lossmodel as L
T = [(150, 1.30), (450, 1.84), (800, 2.48), (900, 2.66), (1800, 4.30), (2000, 4.66), (2400, 5.39), (5000, 10.11)]
def ibw(f):
    for (f0, d0), (f1, d1) in zip(T, T[1:]):
        if f <= f1:
            t = (math.log10(f) - math.log10(f0)) / (math.log10(f1) - math.log10(f0))
            return d0 + t * (d1 - d0)
    return T[-1][1]
_o = A.scale_wall_loss_db
def patched(loss, f, ref=2000.0):
    return ibw(f) if loss == -1 else _o(loss, f, ref)
A.scale_wall_loss_db = patched; L.scale_wall_loss_db = patched
