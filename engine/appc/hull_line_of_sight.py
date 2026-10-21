"""ShipSubsystem.IsHittableFromLocation -- the line-of-fire test the stock AI's
FireScript rates targets with (SDK AI/Preprocessors.py: +1 to a subsystem's
rating when hittable from our ship's location).

Measured against the original (stbc-oracle results_mac/hittable_r20_57_150.json:
137,484 calls of the game's own function, Galaxy and Sovereign, every
subsystem, a 10-degree sphere of shooter points at 20 / 57 / 150 GU):

* The answer is 0 or 1, never fractional, and does not depend on range.
* For every subsystem except energy weapons it is a line of sight through the
  real hull mesh: trace from the shooter toward the subsystem's runtime
  position (ShipSubsystem.GetPosition); hittable iff the trace misses the
  hull altogether or its FIRST hull hit lies within the subsystem's radius of
  that position. 99.7-100% agreement on hull, sensors, shield generator,
  every torpedo tube, impulse pods, nacelles, bridge and engineering; 94.6%
  on the warp core.
* ENERGY WEAPONS (phaser banks, tractor projectors, pulse weapons) follow no
  geometric rule found (12 classes measured; best candidate 77%: the Akira's
  dorsal banks are hittable from everywhere BELOW and nowhere above, the
  Galaxy's ventral banks from nowhere, a Keldon's port beam from almost
  everywhere). Their answer is range-independent (99.2% of cells identical at
  57 and 150 GU), so for the stock ships Dauntless uses the original's own
  measured 10-degree direction map (bc_hittable_maps.json, written by
  stbc-oracle oracle/reports/export_hittable_tables.py), looked up by the
  shooter's direction from the ship's centre. A hardpoint without a measured
  map (mods): phaser banks and tractors 0 (the Galaxy / Sovereign rule),
  pulse weapons the line of sight (the Warbird's and Bird of Prey's 100%).

In the game the trace is the renderer's mesh trace (the hull as drawn). With
no renderer instance (headless runs) the answer stays 1.0 -- the behaviour
before this test existed -- unless DAUNTLESS_HEADLESS_HULL_LOS=1, which loads
the ship's high-LOD NIF through _dauntless_host.nif_shapes (model-space
vertices, BC_MODEL_SCALE 0.01) and traces it with a small CPU BVH here. That
is what the AI-targeting comparison duels use; it is off by default so the
test suite does not pay for meshes it does not look at.
"""
from __future__ import annotations

import os

_ship_instances = None
BC_MODEL_SCALE = 0.01          # host_loop.BC_MODEL_SCALE: NIF units -> GU
_cpu_meshes: dict = {}         # nif path -> _Bvh (or None when unloadable)


def set_ship_instances(mapping) -> None:
    """The current mission's ship -> renderer InstanceId map (host_loop's
    MissionSession.ship_instances, filled in place as ships are realised)."""
    global _ship_instances
    _ship_instances = mapping


def hittable_from(sub, ship, loc) -> float:
    """1.0 when ``sub`` on ``ship`` is in the line of fire from world ``loc``."""
    if ship is None or loc is None:
        return 1.0
    kind = _energy_kind(sub)
    if kind is not None:
        m = measured_hittable(sub, ship, loc)
        if m is not None:
            return m
        if kind != "pulse":
            return 0.0
    if _ship_instances is None or _ship_instances.get(ship) is None:
        if os.environ.get("DAUNTLESS_HEADLESS_HULL_LOS", "") not in ("", "0"):
            return _cpu_hittable(sub, ship, loc)
        return 1.0
    from engine.appc.subsystems import subsystem_world_position
    from engine.appc.math import TGPoint3
    from engine.appc.combat import _resolve_hit_point
    p = subsystem_world_position(sub, ship)
    dx, dy, dz = p.x - loc.x, p.y - loc.y, p.z - loc.z
    dist = (dx * dx + dy * dy + dz * dz) ** 0.5
    if dist <= 1e-6:
        return 1.0
    try:
        radius = float(sub.GetRadius() or 0.0)
    except Exception:  # noqa: BLE001 - a radius-less subsystem tests the point only
        radius = 0.0
    d = TGPoint3(dx / dist, dy / dist, dz / dist)
    try:
        # Long enough that a first hit far beyond the subsystem still counts
        # as "the hull is in the way" (the measured rule uses the infinite ray).
        hit, normal = _resolve_hit_point(_ship_instances, ship, loc, d, dist * 2.0 + 20.0,
                                         None, sphere_fallback=False)
    except Exception:  # noqa: BLE001 - a failed trace must not stop the AI
        return 1.0
    if hit is None or normal is None:
        return 1.0
    hx, hy, hz = hit.x - loc.x, hit.y - loc.y, hit.z - loc.z
    first = (hx * hx + hy * hy + hz * hz) ** 0.5
    return 1.0 if abs(dist - first) <= radius else 0.0


# ── Headless CPU trace (DAUNTLESS_HEADLESS_HULL_LOS=1) ─────────────────────

class _Bvh:
    """Median-split BVH over the hull's triangles, model units."""

    LEAF = 8

    def __init__(self, tris):
        self.tris = tris                       # [(v0, e1, e2)]
        cent = [[(t[0][k] + (t[0][k] + t[1][k]) + (t[0][k] + t[2][k])) / 3.0 for k in range(3)] for t in tris]
        self.nodes = []                        # (lo, hi, left, right, idx list | None)
        self._build(list(range(len(tris))), cent)

    def _bounds(self, idx):
        lo = [1e30] * 3
        hi = [-1e30] * 3
        for i in idx:
            v0, e1, e2 = self.tris[i]
            for p in (v0, [v0[k] + e1[k] for k in range(3)], [v0[k] + e2[k] for k in range(3)]):
                for k in range(3):
                    if p[k] < lo[k]:
                        lo[k] = p[k]
                    if p[k] > hi[k]:
                        hi[k] = p[k]
        return lo, hi

    def _build(self, idx, cent):
        lo, hi = self._bounds(idx)
        n = len(self.nodes)
        self.nodes.append(None)
        if len(idx) <= self.LEAF:
            self.nodes[n] = (lo, hi, -1, -1, idx)
            return n
        ax = max(range(3), key=lambda k: hi[k] - lo[k])
        idx.sort(key=lambda i: cent[i][ax])
        m = len(idx) // 2
        left = self._build(idx[:m], cent)
        right = self._build(idx[m:], cent)
        self.nodes[n] = (lo, hi, left, right, None)
        return n

    def count_hits(self, o, d):
        """Number of triangles the ray o + t*d (t > 0) crosses: the parity is the inside test."""
        return len(self._hits(o, d, first_only=False))

    def first_hit(self, o, d):
        """Distance to the first triangle along the ray o + t*d (d unit), or None."""
        h = self._hits(o, d, first_only=True)
        return min(t for t, _e in h) if h else None

    def first_entry(self, o, d):
        """Distance to the first ENTRY face along the ray (a face whose normal points back at o), or None."""
        h = sorted(self._hits(o, d, first_only=False))
        for t, entry in h:
            if entry:
                return t
        return None

    def _hits(self, o, d, first_only):
        inv = [1.0 / d[k] if abs(d[k]) > 1e-12 else 1e30 for k in range(3)]
        best = None
        found = []
        stack = [0]
        while stack:
            lo, hi, left, right, idx = self.nodes[stack.pop()]
            tmin, tmax = 0.0, 1e30 if (best is None or not first_only) else best
            ok = True
            for k in range(3):
                t1 = (lo[k] - o[k]) * inv[k]
                t2 = (hi[k] - o[k]) * inv[k]
                if t1 > t2:
                    t1, t2 = t2, t1
                if t1 > tmin:
                    tmin = t1
                if t2 < tmax:
                    tmax = t2
                if tmin > tmax:
                    ok = False
                    break
            if not ok:
                continue
            if idx is None:
                stack.append(left)
                stack.append(right)
                continue
            for i in idx:
                v0, e1, e2 = self.tris[i]
                px = d[1] * e2[2] - d[2] * e2[1]
                py = d[2] * e2[0] - d[0] * e2[2]
                pz = d[0] * e2[1] - d[1] * e2[0]
                det = e1[0] * px + e1[1] * py + e1[2] * pz
                if -1e-12 < det < 1e-12:
                    continue
                f = 1.0 / det
                sx, sy, sz = o[0] - v0[0], o[1] - v0[1], o[2] - v0[2]
                u = (sx * px + sy * py + sz * pz) * f
                if u < 0.0 or u > 1.0:
                    continue
                qx = sy * e1[2] - sz * e1[1]
                qy = sz * e1[0] - sx * e1[2]
                qz = sx * e1[1] - sy * e1[0]
                v = (d[0] * qx + d[1] * qy + d[2] * qz) * f
                if v < 0.0 or u + v > 1.0:
                    continue
                t = (e2[0] * qx + e2[1] * qy + e2[2] * qz) * f
                if t > 1e-6:
                    # det > 0: the face's normal (e1 x e2) points back along the ray -- an ENTRY face.
                    found.append((t, det > 0.0))
                    if best is None or t < best:
                        best = t
        return found


def _nif_path(ship):
    try:
        from engine.host_loop import _ship_nif_path
        return _ship_nif_path(ship)
    except Exception:  # noqa: BLE001 - no NIF -> no trace
        return None


def _cpu_mesh(ship):
    path = _nif_path(ship)
    if not path:
        return None
    if path not in _cpu_meshes:
        bvh = None
        try:
            import _dauntless_host as h
            tris = []
            for shp in h.nif_shapes(path):
                if shp.get("hidden"):
                    continue
                V = shp["vertices"]
                for a, b, c in shp["triangles"]:
                    v0, v1, v2 = V[a], V[b], V[c]
                    tris.append((tuple(v0), tuple(v1[k] - v0[k] for k in range(3)),
                                 tuple(v2[k] - v0[k] for k in range(3))))
            bvh = _Bvh(tris) if tris else None
        except Exception:  # noqa: BLE001
            bvh = None
        _cpu_meshes[path] = bvh
    return _cpu_meshes[path]


def _cpu_hittable(sub, ship, loc) -> float:
    bvh = _cpu_mesh(ship)
    if bvh is None:
        return 1.0
    from engine.appc.subsystems import subsystem_world_position
    try:
        scale = BC_MODEL_SCALE * float(ship.GetScale() or 1.0)
        w = ship.GetWorldLocation()
        R = ship.GetWorldRotation()
        cols = [R.GetCol(i) for i in range(3)]
    except Exception:  # noqa: BLE001
        return 1.0

    def body(p):                               # world -> model units
        dx, dy, dz = p.x - w.x, p.y - w.y, p.z - w.z
        return [(c.x * dx + c.y * dy + c.z * dz) / scale for c in cols]

    o = body(loc)
    P = body(subsystem_world_position(sub, ship))
    dv = [P[k] - o[k] for k in range(3)]
    dist = (dv[0] ** 2 + dv[1] ** 2 + dv[2] ** 2) ** 0.5
    if dist <= 1e-9:
        return 1.0
    d = [x / dist for x in dv]
    first = bvh.first_hit(o, d)
    if first is None:
        return 1.0
    try:
        radius = float(sub.GetRadius() or 0.0) / scale
    except Exception:  # noqa: BLE001
        radius = 0.0
    return 1.0 if abs(dist - first) <= radius else 0.0


# ── Damage point (ShipSubsystem.GetDamagePoint) ────────────────────────────
#
# Measured (stbc-oracle catalogues: every subsystem of ten classes -- Galaxy,
# Sovereign, Akira, Nebula, Ambassador, Warbird, Bird of Prey, Vor'cha, Keldon,
# Galor, Kessok Heavy, Marauder): a subsystem's damage point -- the centre a
# hit's footprint is measured from -- is its position, unless that position
# lies OUTSIDE the hull (a phaser bank's strip midpoint, a tube on the skin):
# then the original walks from the position toward the ship's centre to the
# first ENTRY face of the hull and goes a further 0.050 GU in. 302 of 307
# subsystems to 0.005 GU. The Galaxy's Ventral Phaser 1 / 4 and Forward
# Tractor 2 walk outside the hull almost to the centre (damage points
# ~(0, 0, 0)); the Bird of Prey's hull and warp core walk THROUGH the centre to
# the far side. Misses: the Galaxy's Aft Tractor 2 and the Marauder's aft
# tractor (0.05 GU outside the drawn mesh, inside the original's coarser hull
# mask: kept there, moved 0.1 GU here) and three Bird of Prey points where its
# mesh has doubled faces. "Outside" is a majority of ray-crossing parities (the
# drawn hulls are not watertight).

DAMAGE_POINT_DEPTH_GU = 0.050
_INSIDE_DIRS = tuple(tuple(c / (sum(x * x for x in v) ** 0.5) for c in v) for v in (
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
    (1, 1, 1), (-1, -1, 1), (1, -1, -1), (-1, 1, -1), (0.3, 0.9, 0.2)))
_damage_points: dict = {}      # (nif path, subsystem name, position) -> (x, y, z) model frame, GU


def _inside(bvh, p) -> bool:
    """Majority vote of ray-crossing parities."""
    votes = sum(bvh.count_hits(p, d) % 2 for d in _INSIDE_DIRS)
    return votes * 2 > len(_INSIDE_DIRS)


def damage_point_local(sub, ship):
    """The original's damage point, ship model frame (GU), or None (no hull geometry:
    callers keep the position)."""
    if ship is None:
        return None
    path = _nif_path(ship)
    if not path:
        return None
    pos = sub.GetPositionTG()
    key = (path, sub.GetName(), round(pos.x, 5), round(pos.y, 5), round(pos.z, 5))
    if key in _damage_points:
        return _damage_points[key]
    bvh = _cpu_mesh(ship)
    if bvh is None:
        return None
    s = BC_MODEL_SCALE
    P = (pos.x / s, pos.y / s, pos.z / s)
    n = (P[0] ** 2 + P[1] ** 2 + P[2] ** 2) ** 0.5
    out = (pos.x, pos.y, pos.z)
    if n > 1e-9 and not _inside(bvh, P):
        d = (-P[0] / n, -P[1] / n, -P[2] / n)
        t = bvh.first_entry(P, d)
        if t is not None:
            k = t + DAMAGE_POINT_DEPTH_GU / s
            out = tuple((P[i] + d[i] * k) * s for i in range(3))
    _damage_points[key] = out
    return out


# ── Measured maps for energy weapons ───────────────────────────────────────

_maps_cache: dict = {}
_hardpoints: dict = {}         # ship script -> hardpoint file name (lower case) or None


def _energy_kind(sub):
    """'phaser' / 'tractor' / 'pulse' for an energy weapon, else None."""
    try:
        from engine.appc.weapon_subsystems import PhaserBank, PulseWeapon, TractorBeam
    except Exception:  # noqa: BLE001
        return None
    if isinstance(sub, PulseWeapon):
        return "pulse"
    if isinstance(sub, TractorBeam):
        return "tractor"
    if isinstance(sub, PhaserBank):
        return "phaser"
    return None


def _maps():
    if "m" not in _maps_cache:
        import json
        path = os.path.join(os.path.dirname(__file__), "bc_hittable_maps.json")
        try:
            with open(path) as f:
                _maps_cache["m"] = json.load(f)
        except Exception:  # noqa: BLE001 - no maps: every energy weapon uses the fallback
            _maps_cache["m"] = {"ships": {}}
    return _maps_cache["m"]


def _hardpoint(ship):
    try:
        script = ship.GetScript()
    except Exception:  # noqa: BLE001
        return None
    if script not in _hardpoints:
        hp = None
        try:
            import importlib
            mod = importlib.import_module(script)
            hp = str(mod.GetShipStats()["HardpointFile"]).lower()
        except Exception:  # noqa: BLE001
            hp = None
        _hardpoints[script] = hp
    return _hardpoints[script]


def measured_hittable(sub, ship, loc):
    """The original's measured answer for this energy weapon from ``loc`` (1.0 / 0.0), or None when its
    hardpoint has no measured map."""
    import math
    hp = _hardpoint(ship)
    table = _maps()["ships"].get(hp) if hp else None
    if not table:
        return None
    bits = table.get(sub.GetName())
    if not bits:
        return None
    try:
        w = ship.GetWorldLocation()
        R = ship.GetWorldRotation()
        cols = [R.GetCol(i) for i in range(3)]
    except Exception:  # noqa: BLE001
        return None
    dx, dy, dz = loc.x - w.x, loc.y - w.y, loc.z - w.z
    x, y, z = (c.x * dx + c.y * dy + c.z * dz for c in cols)
    r = (x * x + y * y + z * z) ** 0.5
    if r < 1e-9:
        return 1.0
    elev = math.degrees(math.asin(max(-1.0, min(1.0, z / r))))
    az = math.degrees(math.atan2(x, y)) % 360.0
    row = min(18, max(0, int(round((elev + 90.0) / 10.0))))
    col = int(round(az / 10.0)) % 36
    digit = int(bits[row * 9 + col // 4], 16)
    return 1.0 if (digit >> (col % 4)) & 1 else 0.0
