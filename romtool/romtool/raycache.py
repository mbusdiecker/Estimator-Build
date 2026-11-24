"""Cached, vectorized wall-crossing counts for the physics loss mode.

Which walls the straight ray antenna -> grid point crosses depends only on
geometry -- not on the wall loss in dB, the threshold, the topology or the
remote count. So the per-point crossing counts per wall material are computed
once per (antenna, grid) pair, cached, and re-used by every later call
(``_contrib``, the planner's coverage loop, ``predict_floor``).

The count rule is exactly ``geometry.seg_cross``: a strict interior
intersection of the open segments. Shared endpoints, collinear overlaps, a ray
ending on a wall and zero-length segments are never crossings; the vectorized
and JIT paths reproduce ``seg_cross`` operation-for-operation (same order of
the same IEEE double operations, no fast-math) so borderline cases decide the
same way.

Environment switches:
- ``ROMTOOL_SLOW_RAYS=1``  callers keep the old per-point
  ``ray_wall_crossings`` loop (results must be identical; used to pin
  behaviour).
- ``ROMTOOL_NO_NUMBA=1``   force the numpy path even when numba imports.
- ``ROMTOOL_RAY_CACHE_MB`` counts-cache memory cap in MiB (default 1024;
  0 disables caching).
"""

import hashlib
import math
import os
import struct
import threading
from collections import OrderedDict

import numpy as np

from .geometry import build_wall_index, ray_wall_crossings

__all__ = ["WallRays", "clear_ray_cache", "ray_cache_stats",
           "set_cache_limit_mb", "slow_rays", "no_numba",
           "wall_geometry_hash"]


def slow_rays():
    """True when ROMTOOL_SLOW_RAYS asks for the old per-point ray loop."""
    return os.environ.get("ROMTOOL_SLOW_RAYS", "").strip().lower() in ("1", "true", "yes")


def no_numba():
    """True when ROMTOOL_NO_NUMBA forces the numpy path."""
    return os.environ.get("ROMTOOL_NO_NUMBA", "").strip().lower() in ("1", "true", "yes")


# --------------------------------------------------------------- wall hash

def wall_geometry_hash(walls):
    """Stable digest of the wall geometry: coordinates and materials only."""
    h = hashlib.blake2b(digest_size=16, person=b"rom-walls-v4")
    h.update(b"%d" % len(walls))
    for w in walls:
        try:
            vals = (float(w["x1"]), float(w["y1"]), float(w["x2"]), float(w["y2"]))
        except (KeyError, TypeError, ValueError):
            vals = (0.0, 0.0, 0.0, 0.0)
        h.update(struct.pack("<4d", *vals))
        h.update(repr(w.get("material")).encode("utf-8"))
        h.update(b"|")
    return h.digest()


def _wall_array(walls):
    """(W, 4) float64 array of wall endpoints in wall-list order."""
    arr = np.empty((len(walls), 4), np.float64)
    for i, w in enumerate(walls):
        arr[i, 0] = float(w["x1"])
        arr[i, 1] = float(w["y1"])
        arr[i, 2] = float(w["x2"])
        arr[i, 3] = float(w["y2"])
    return arr


# ----------------------------------------------------------- spatial index

class _SpatialIndex(object):
    """Uniform cell grid over the walls' bounding box.

    A wall is inserted into every cell whose closed box meets the wall's
    bounding box. For a ray, the candidate walls are the union of the cell
    lists along the ray (Amanatides-Woo walk): any wall the ray strictly
    crosses contains the crossing point, which lies in a cell the walk
    visits, and the wall's bounding box meets that cell -- so the walk is a
    conservative superset of the crossings. Walls are de-duplicated per ray.
    """

    __slots__ = ("ox", "oy", "cs", "ncols", "nrows", "cstart", "cwalls", "cell_capacity")


def _build_spatial_index(walls, w_ar):
    """Bucket the walls into cells; cell size from the floor size and count."""
    n = len(walls)
    x1, y1, x2, y2 = w_ar[:, 0], w_ar[:, 1], w_ar[:, 2], w_ar[:, 3]
    wminx = np.minimum(x1, x2)
    wmaxx = np.maximum(x1, x2)
    wminy = np.minimum(y1, y2)
    wmaxy = np.maximum(y1, y2)
    gminx = float(wminx.min())
    gminy = float(wminy.min())
    bw = max(float(wmaxx.max()) - gminx, 1.0)
    bh = max(float(wmaxy.max()) - gminy, 1.0)
    # enough cells that the average one holds a few walls (counting the
    # multi-cell insertion of long walls)
    cs = max(1.0, math.sqrt(3.0 * bw * bh / max(n, 1)) / 2.0)
    max_cells = 1 << 18
    while True:
        ncols = max(1, int(math.ceil(bw / cs)))
        nrows = max(1, int(math.ceil(bh / cs)))
        if ncols * nrows <= max_cells:
            break
        cs *= 1.5
    cell_lists = [[] for _ in range(ncols * nrows)]
    for i in range(n):
        c0 = int(math.floor((float(wminx[i]) - gminx) / cs))
        c1 = int(math.floor((float(wmaxx[i]) - gminx) / cs))
        r0 = int(math.floor((float(wminy[i]) - gminy) / cs))
        r1 = int(math.floor((float(wmaxy[i]) - gminy) / cs))
        c0 = min(max(c0, 0), ncols - 1)
        c1 = min(max(c1, 0), ncols - 1)
        r0 = min(max(r0, 0), nrows - 1)
        r1 = min(max(r1, 0), nrows - 1)
        for r in range(r0, r1 + 1):
            base = r * ncols
            for c in range(c0, c1 + 1):
                cell_lists[base + c].append(i)
    lengths = np.fromiter((len(cl) for cl in cell_lists),
                          count=ncols * nrows, dtype=np.int64)
    cstart = np.zeros(ncols * nrows + 1, np.int64)
    np.cumsum(lengths, out=cstart[1:])
    cwalls = np.empty(int(cstart[-1]), np.int64)
    pos = 0
    for cl in cell_lists:
        k = len(cl)
        if k:
            cwalls[pos:pos + k] = cl
            pos += k
    idx = _SpatialIndex()
    idx.ox = gminx
    idx.oy = gminy
    idx.cs = cs
    idx.ncols = ncols
    idx.nrows = nrows
    idx.cstart = cstart
    idx.cwalls = cwalls
    idx.cell_capacity = 1024   # per-ray candidate buffer; overflow -> full scan
    return idx


_INDEX_CACHE = OrderedDict()      # wall hash -> _SpatialIndex
_INDEX_CACHE_MAX = 4


def _spatial_index(walls, w_ar, whash):
    with _LOCK:
        idx = _INDEX_CACHE.get(whash)
        if idx is not None:
            _INDEX_CACHE.move_to_end(whash)
            return idx
    idx = _build_spatial_index(walls, w_ar)
    with _LOCK:
        _INDEX_CACHE[whash] = idx
        while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)
    return idx


# ------------------------------------------------------------------ caches

_LOCK = threading.RLock()
_CACHE = OrderedDict()            # key -> (nbytes, {material: uint16 row})
_STATS = {"hits": 0, "misses": 0, "stores": 0, "evictions": 0, "bytes": 0, "oversize_skips": 0}
_LIMIT_BYTES = None               # resolved from the env on first use
_GRID_MEMO = OrderedDict()        # (id(xs), id(ys), len) -> (refs, arrays, digest)
_GRID_MEMO_MAX = 4


def _resolve_limit():
    global _LIMIT_BYTES
    if _LIMIT_BYTES is None:
        raw = os.environ.get("ROMTOOL_RAY_CACHE_MB", "1024")
        try:
            mb = float(raw)
        except ValueError:
            mb = 1024.0
        _LIMIT_BYTES = max(0.0, mb) * 1024.0 * 1024.0
    return _LIMIT_BYTES


def set_cache_limit_mb(mb):
    """Set the counts-cache memory cap in MiB (0 disables caching)."""
    global _LIMIT_BYTES
    with _LOCK:
        _LIMIT_BYTES = max(0.0, float(mb)) * 1024.0 * 1024.0
        _prune_locked()


def clear_ray_cache():
    """Drop every cached crossing count and spatial index; reset the stats."""
    global _LIMIT_BYTES
    with _LOCK:
        _CACHE.clear()
        _INDEX_CACHE.clear()
        _GRID_MEMO.clear()
        _LIMIT_BYTES = None
        for key in _STATS:
            _STATS[key] = 0


def ray_cache_stats():
    """Snapshot of the cache counters (hits, misses, stores, evictions...)."""
    with _LOCK:
        return dict(_STATS)


def cache_stats_simple():
    """Short-key snapshot for tests: hit/miss/evict/bytes/stores."""
    with _LOCK:
        return {"hit": _STATS["hits"], "miss": _STATS["misses"],
                "evict": _STATS["evictions"], "bytes": _STATS["bytes"],
                "stores": _STATS["stores"],
                "skips": _STATS["oversize_skips"]}


def _prune_locked():
    cap = _resolve_limit()
    while _STATS["bytes"] > cap and _CACHE:
        key, (nbytes, _val) = _CACHE.popitem(last=False)
        _STATS["bytes"] -= nbytes
        _STATS["evictions"] += 1


def _grid_arrays(xs, ys):
    """Convert the grid point lists to float64 arrays; memoize the digest.

    Memo entries hold references to the lists, so an id-keyed lookup can
    never hit a different object; the content digest feeds the cache key.
    """
    key = (id(xs), id(ys), len(xs), len(ys))
    with _LOCK:
        hit = _GRID_MEMO.get(key)
        if hit is not None and hit[0] is xs and hit[1] is ys:
            _GRID_MEMO.move_to_end(key)
            return hit[2], hit[3], hit[4]
    arr_x = np.asarray(xs, np.float64)
    arr_y = np.asarray(ys, np.float64)
    if arr_x.ndim != 1 or arr_y.ndim != 1 or arr_x.shape != arr_y.shape:
        raise ValueError("grid xs/ys must be equal-length 1-D sequences")
    h = hashlib.blake2b(digest_size=16, person=b"rom-grid-v4")
    h.update(arr_x.tobytes())
    h.update(arr_y.tobytes())
    digest = h.digest()
    with _LOCK:
        _GRID_MEMO[key] = (xs, ys, arr_x, arr_y, digest)
        while len(_GRID_MEMO) > _GRID_MEMO_MAX:
            _GRID_MEMO.popitem(last=False)
    return arr_x, arr_y, digest


# ------------------------------------------------------ numba (optional) _JIT

_JIT = {"kernel": None, "checked": False, "failed": False}


def _numba_kernel():
    """The parallel JIT kernel, or None when numba is missing/disabled."""
    if no_numba() or _JIT["failed"]:
        return None
    if not _JIT["checked"]:
        _JIT["checked"] = True
        try:
            import numba
        except Exception:
            return None
        _JIT["kernel"] = _make_kernel(numba)
    return _JIT["kernel"]


def _make_kernel(numba):
    nb = numba

    @nb.njit(parallel=True, cache=True, fastmath=False)
    def kernel(ax, ay, xs_arr, ys_arr, sx, sy, qx, qy, slot,
               ox, oy, cs, ncols, nrows, cstart, cwalls, n_slots, guard_max,
               out):
        P = xs_arr.shape[0]
        W = sx.shape[0]
        cap = 1024
        if W < cap:
            cap = W
        for p in nb.prange(P):
            x = xs_arr[p]
            y = ys_arr[p]
            rx = x - ax
            ry = y - ay
            if rx * rx + ry * ry == 0.0:
                continue
            cand = np.empty(cap, np.int64)
            nc = 0
            overflow = False
            col = int(math.floor((ax - ox) / cs))
            row = int(math.floor((ay - oy) / cs))
            ecol = int(math.floor((x - ox) / cs))
            erow = int(math.floor((y - oy) / cs))
            if rx > 0.0:
                step_x = 1
                t_max_x = (ox + (col + 1) * cs - ax) / rx
                t_dx = cs / rx
            elif rx < 0.0:
                step_x = -1
                t_max_x = (ox + col * cs - ax) / rx
                t_dx = -cs / rx
            else:
                step_x = 0
                t_max_x = 1.0e308
                t_dx = 1.0e308
            if ry > 0.0:
                step_y = 1
                t_max_y = (oy + (row + 1) * cs - ay) / ry
                t_dy = cs / ry
            elif ry < 0.0:
                step_y = -1
                t_max_y = (oy + row * cs - ay) / ry
                t_dy = -cs / ry
            else:
                step_y = 0
                t_max_y = 1.0e308
                t_dy = 1.0e308

            # visit(cell) inline, twice (start cell + every step)
            if 0 <= col < ncols and 0 <= row < nrows:
                cid = row * ncols + col
                s0 = cstart[cid]
                s1 = cstart[cid + 1]
                for kk in range(s0, s1):
                    wid = cwalls[kk]
                    dup = False
                    for jj in range(nc):
                        if cand[jj] == wid:
                            dup = True
                            break
                    if not dup:
                        if nc < cap:
                            cand[nc] = wid
                            nc += 1
                        else:
                            overflow = True
            guard = 0
            while True:
                if col == ecol and row == erow:
                    break
                if t_max_x <= t_max_y:
                    if t_max_x >= 1.0:
                        break
                    col += step_x
                    t_max_x += t_dx
                else:
                    if t_max_y >= 1.0:
                        break
                    row += step_y
                    t_max_y += t_dy
                if 0 <= col < ncols and 0 <= row < nrows:
                    cid = row * ncols + col
                    s0 = cstart[cid]
                    s1 = cstart[cid + 1]
                    for kk in range(s0, s1):
                        wid = cwalls[kk]
                        dup = False
                        for jj in range(nc):
                            if cand[jj] == wid:
                                dup = True
                                break
                        if not dup:
                            if nc < cap:
                                cand[nc] = wid
                                nc += 1
                            else:
                                overflow = True
                guard += 1
                if guard > guard_max:
                    overflow = True
                    break

            if overflow:
                # candidate buffer too small for this ray: full scan (each
                # wall once, same exact test)
                for w in range(W):
                    denom = rx * sy[w] - ry * sx[w]
                    if denom == 0.0:
                        continue
                    t = (qx[w] * sy[w] - qy[w] * sx[w]) / denom
                    if t > 0.0 and t < 1.0:
                        u = (qx[w] * ry - qy[w] * rx) / denom
                        if u > 0.0 and u < 1.0:
                            out[p, slot[w]] += 1
            else:
                for jj in range(nc):
                    w = cand[jj]
                    denom = rx * sy[w] - ry * sx[w]
                    if denom == 0.0:
                        continue
                    t = (qx[w] * sy[w] - qy[w] * sx[w]) / denom
                    if t > 0.0 and t < 1.0:
                        u = (qx[w] * ry - qy[w] * rx) / denom
                        if u > 0.0 and u < 1.0:
                            out[p, slot[w]] += 1

    return kernel


# ------------------------------------------------------------ compute paths

def _counts_via_numpy(w_ar, sx, sy, qx, qy, t_num, slot_arr, n_slots,
                      xs_arr, ys_arr, ax, ay):
    """Chunked, fully vectorized seg_cross over every (point, wall) pair.

    Same operations, same order, same IEEE doubles as ``seg_cross``:
    denom = rx*sy - ry*sx; t = (qx*sy - qy*sx)/denom; u = (qx*ry - qy*rx)/denom.
    A zero denom, NaN (inf-inf) or a comparison outside (0, 1) is not a
    crossing, exactly like the scalar code.
    """
    P = xs_arr.shape[0]
    W = w_ar.shape[0]
    out = np.zeros((P, n_slots), np.uint16)
    if W == 0 or P == 0:
        return out
    rx = xs_arr - ax
    ry = ys_arr - ay
    col_masks = [slot_arr == s for s in range(n_slots)]
    chunk = int(2_000_000 // max(W, 1))
    chunk = max(16, min(chunk, 2000, P))
    for c0 in range(0, P, chunk):
        c1 = min(P, c0 + chunk)
        rx_c = rx[c0:c1][:, None]
        ry_c = ry[c0:c1][:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = rx_c * sy[None, :] - ry_c * sx[None, :]
            t = t_num[None, :] / denom
            u = (qx[None, :] * ry_c - qy[None, :] * rx_c) / denom
        cross = (denom != 0.0) & (t > 0.0) & (t < 1.0) & (u > 0.0) & (u < 1.0)
        blk = out[c0:c1]
        for s in range(n_slots):
            cnt = cross[:, col_masks[s]].sum(axis=1)
            blk[:, s] = np.minimum(cnt, 65535).astype(np.uint16)
    return out


def _counts_pair(walls, w_ar, whash, ax, ay, xs_arr, ys_arr, mats_order,
                 backend):
    """Per-material uint16 count rows for one antenna over the whole grid."""
    W = w_ar.shape[0]
    n_slots = len(mats_order)
    if W == 0:
        return {}
    slot_arr = np.empty(W, np.int64)
    seen = {}
    for i, w in enumerate(walls):
        slot_arr[i] = seen.setdefault(w.get("material"), len(seen))
    x1, y1, x2, y2 = w_ar[:, 0], w_ar[:, 1], w_ar[:, 2], w_ar[:, 3]
    sx = x2 - x1
    sy = y2 - y1
    qx = x1 - ax
    qy = y1 - ay
    t_num = qx * sy - qy * sx           # wall-constant numerator of t
    out = None
    if backend == "numba":
        kernel = _numba_kernel()
        if kernel is not None:
            try:
                idx = _spatial_index(walls, w_ar, whash)
                out32 = np.zeros((xs_arr.shape[0], n_slots), np.int32)
                guard_max = 4 * (idx.ncols + idx.nrows) + 16
                kernel(float(ax), float(ay), xs_arr, ys_arr, sx, sy, qx, qy,
                       slot_arr, float(idx.ox), float(idx.oy), float(idx.cs),
                       int(idx.ncols), int(idx.nrows), idx.cstart, idx.cwalls,
                       int(n_slots), int(guard_max), out32)
                out = out32
            except Exception:
                _JIT["failed"] = True      # fall back permanently
                out = None
    if out is None:
        out = _counts_via_numpy(w_ar, sx, sy, qx, qy, t_num, slot_arr,
                                n_slots, xs_arr, ys_arr, ax, ay)
    packed = {}
    for s, mat in enumerate(mats_order):
        packed[mat] = np.ascontiguousarray(out[:, s]).astype(np.uint16)
    return packed


# ------------------------------------------------------------------ WallRays

class WallRays(object):
    """One floor's walls, ready for both ray paths.

    ``.index`` is the legacy ``build_wall_index`` list (the ROMTOOL_SLOW_RAYS
    path keeps using it); ``counts(ax, ay, xs, ys)`` returns per-point
    crossing counts per wall material, cached. Wall loss values never enter
    the counts or the cache key: the caller turns counts into dB.
    """

    def __init__(self, walls):
        self.walls = list(walls)
        self.index = build_wall_index(self.walls)
        self.hash = wall_geometry_hash(self.walls)
        mats = []
        seen = set()
        for w in self.walls:
            m = w.get("material")
            if m not in seen:
                seen.add(m)
                mats.append(m)
        self.mats_order = mats
        self._w_ar = None

    def __bool__(self):
        return bool(self.walls)

    def _wall_array(self):
        if self._w_ar is None:
            self._w_ar = _wall_array(self.walls)
        return self._w_ar

    def counts(self, ax, ay, xs, ys, force_backend=None, use_cache=True):
        """Per-material crossing counts for the whole grid.

        Returns {material: uint16 ndarray} aligned with the xs/ys point
        order (a full grid -> same order as predict/autoplace use). Arrays
        are read-only; treat them as immutable.
        """
        if not self.walls:
            return {}
        arr_x, arr_y, ghash = _grid_arrays(xs, ys)
        ax = float(ax)
        ay = float(ay)
        key = (self.hash, ghash, ax, ay)
        if use_cache:
            with _LOCK:
                hit = _CACHE.get(key)
                if hit is not None:
                    _STATS["hits"] += 1
                    _CACHE.move_to_end(key)
                    return hit[1]
                _STATS["misses"] += 1
        if force_backend is not None:
            backend = force_backend
        else:
            backend = "numba" if (_numba_kernel() is not None) else "numpy"
            if _JIT["failed"]:
                backend = "numpy"
        val = _counts_pair(self.walls, self._wall_array(), self.hash,
                           ax, ay, arr_x, arr_y, self.mats_order, backend)
        for row in val.values():
            row.flags.writeable = False
        if use_cache:
            with _LOCK:
                nbytes = arr_x.shape[0] * 2 * max(len(val), 1) + 256
                cap = _resolve_limit()
                _STATS["stores"] += 1
                if cap > 0 and nbytes <= cap:
                    _CACHE[key] = (nbytes, val)
                    _STATS["bytes"] += nbytes
                    _prune_locked()
                else:
                    _STATS["oversize_skips"] += 1
        return val
