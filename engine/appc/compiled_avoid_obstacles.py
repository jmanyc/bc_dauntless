"""The compiled AvoidObstacles node, as the shipped game runs it.

Written from cleanroom/spec/02_AvoidObstacles_compiled.md. Like FireScript, the
SDK's ``AI.Preprocessors.AvoidObstacles`` is replaced in the shipped game by an
engine node, and that node reads NONE of the Python instance's settings
(``fPredictionTime``, ``fPersonalSpace``, the update delays): it uses the
constants below and asks to be updated again at once, so it runs every time
the AI tree reaches it.

``CompiledAvoidObstaclesMixin`` is mixed in ahead of the (non-lethal) SDK class
by engine/appc/ai_optimized.py, which binds it as the default. The SDK-script
binding stays available there for comparison and for tests that pin it.

Dauntless policies kept on top of the spec (not from the original):

* a pair with collisions disabled between them (``EnableCollisionsWith``, e.g.
  a ship docking with a starbase) is never an obstacle;
* large rock-field rocks, which are not set objects, are offered as static
  obstacles through the same must-avoid test;
* no ship -> "run the contained AI" (the non-lethal rule), never "done".
"""

import math

import App

from engine.appc.math import TGPoint3

# ── Constants ────────────────────────────────────────────────────────────────
LOOK_AHEAD_S = 10.0
STEP_S = 0.5
STEPS = 20
# Personal-space factor: 2.5 at speeds up to 0.1 GU/s, 4.0 at 2.0 GU/s and
# above, linear between.
PERSONAL_SPACE_LOW = 2.5
PERSONAL_SPACE_HIGH = 4.0
PERSONAL_SPACE_LOW_SPEED = 0.1
PERSONAL_SPACE_HIGH_SPEED = 2.0
MINIMUM_QUERY_RADIUS_GU = 225.0
BIG_OBSTACLE_GU = 25.0
BIG_INFLATE_MULT = 1.25
BIG_INFLATE_ADD_GU = 125.0
MEMORY_S = 1.0
FAVOURABILITY_K = -50.0
BLOCKED_FACTOR = 3.25
FORWARD_FACTOR = -0.25
MOVING_SPEED = 0.0625
AVOID_IMPULSE_POWER = 1.25

_MEMORY_ATTR = "_cao_memory"          # {obj id: (obj, stamp)}
_SAVED_POWER_ATTR = "_cao_saved_power"


def _now() -> float:
    try:
        return float(App.g_kUtopiaModule.GetGameTime())
    except Exception:
        return 0.0


def _v(p):
    return (float(p.x), float(p.y), float(p.z))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _len(a):
    return math.sqrt(_dot(a, a))


def _scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _unit(a):
    n = _len(a)
    if n < 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def personal_space_factor(speed: float) -> float:
    if speed <= PERSONAL_SPACE_LOW_SPEED:
        return PERSONAL_SPACE_LOW
    if speed >= PERSONAL_SPACE_HIGH_SPEED:
        return PERSONAL_SPACE_HIGH
    f = (speed - PERSONAL_SPACE_LOW_SPEED) / (PERSONAL_SPACE_HIGH_SPEED - PERSONAL_SPACE_LOW_SPEED)
    return PERSONAL_SPACE_LOW + f * (PERSONAL_SPACE_HIGH - PERSONAL_SPACE_LOW)


def effective_radius(r: float) -> float:
    """r, or for a big obstacle (r over 25 GU) min(1.25 r, r + 125)."""
    if r > BIG_OBSTACLE_GU:
        return min(BIG_INFLATE_MULT * r, r + BIG_INFLATE_ADD_GU)
    return r


def must_avoid(p, v, a, R, o, w, b, r) -> bool:
    """Whether an obstacle (centre o, velocity w, acceleration b, radius r)
    must be avoided by a ship at p with velocity v, acceleration a and
    personal space R. Tuples of three floats."""
    if _len(_sub(o, p)) < R + effective_radius(r):
        return True
    ps, vs, po, vo = p, v, o, w
    h = STEP_S
    hh = 0.5 * h * h
    R2 = R * R
    for _ in range(STEPS):
        ps1 = (ps[0] + vs[0] * h + a[0] * hh, ps[1] + vs[1] * h + a[1] * hh,
               ps[2] + vs[2] * h + a[2] * hh)
        po1 = (po[0] + vo[0] * h + b[0] * hh, po[1] + vo[1] * h + b[1] * hh,
               po[2] + vo[2] * h + b[2] * hh)
        r0 = _sub(po, ps)
        r1 = _sub(po1, ps1)
        d = _sub(r1, r0)
        dd = _dot(d, d)
        f = 0.0 if dd < 1e-18 else max(0.0, min(1.0, -_dot(r0, d) / dd))
        closest = (r0[0] + d[0] * f, r0[1] + d[1] * f, r0[2] + d[2] * f)
        if _dot(closest, closest) < R2:
            return True
        ps, po = ps1, po1
        vs = (vs[0] + a[0] * h, vs[1] + a[1] * h, vs[2] + a[2] * h)
        vo = (vo[0] + b[0] * h, vo[1] + b[1] * h, vo[2] + b[2] * h)
    return False


_LATTICE = tuple(
    _unit((float(x), float(y), float(z)))
    for x in (-1, 0, 1) for y in (-1, 0, 1) for z in (-1, 0, 1)
    if (x, y, z) != (0, 0, 0))


def _obstacle_info(p, ship_r, o, w, r, forward):
    """(direction, velocity, favourability, cone) for one listed obstacle."""
    rel = _sub(o, p)
    dist = _len(rel)
    direction = _unit(rel) or forward
    dist = max(dist, 1e-9)
    fav = FAVOURABILITY_K / dist
    rs = r + ship_r
    cone = 0.0 if dist <= rs else math.sqrt(dist * dist - rs * rs) / dist
    return direction, w, fav, cone


def appeal(t, forward, infos) -> float:
    total = 0.0
    for direction, w, fav, cone in infos:
        if _dot(t, direction) >= cone:
            total += BLOCKED_FACTOR * fav
            continue
        speed = _len(w)
        if speed > MOVING_SPEED:
            wd = _scale(w, 1.0 / speed)
            total += 2.0 * fav * (abs(_dot(t, wd)) - 0.5)
            tp = _unit(_sub(t, _scale(wd, _dot(t, wd))))
            dp = _unit(_sub(direction, _scale(wd, _dot(direction, wd))))
            c = _dot(tp, dp) if (tp is not None and dp is not None) else 0.0
            if c >= 0.0:
                total += 2.0 * fav * (c - 0.5)
            else:
                total += fav * (-1.0 - c)
        else:
            total += 2.0 * fav * ((_dot(t, direction) + 1.0) / (cone + 1.0) - 0.5)
        total += FORWARD_FACTOR * fav * _dot(t, forward)
    return total


def choose_flee(forward, infos):
    """(direction, speed): the most appealing candidate (the opposite of each
    obstacle's direction, then the 26 cube-lattice directions; the earliest
    wins a tie); speed 0 when forward lies inside any obstacle's cone."""
    candidates = [_scale(d, -1.0) for d, _w, _f, _c in infos] + list(_LATTICE)
    best, best_score = None, None
    for t in candidates:
        score = appeal(t, forward, infos)
        if best_score is None or score > best_score:
            best, best_score = t, score
    blocked = any(_dot(forward, d) >= cone for d, _w, _f, cone in infos)
    return best, (0.0 if blocked else 1.0)


def _physics_motion(obj):
    """(velocity, acceleration) as tuples; zero for an object without physics."""
    from engine.appc.objects import PhysicsObjectClass
    if not isinstance(obj, PhysicsObjectClass):
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    from engine.appc.collision_avoidance import _world_velocity
    v = _v(_world_velocity(obj))
    try:
        b = _v(obj.GetAccelerationTG())
    except Exception:
        b = (0.0, 0.0, 0.0)
    return v, b


def _is_cloaked(obj) -> bool:
    from engine.appc.ships import ShipClass
    if not isinstance(obj, ShipClass):
        return False
    try:
        return bool(obj.IsCloaked())
    except Exception:
        return False


def evaluate_course(node, ship):
    """The node's course test: (direction tuple, speed) or (None, None)."""
    from engine.appc.collisions import _collision_disabled_ids, world_radius
    from engine.appc.collision_avoidance import obstacle_snapshot
    pSet = ship.GetContainingSet()
    if pSet is None:
        return None, None
    p = _v(ship.GetWorldLocation())
    v, a = _physics_motion(ship)
    ship_r = float(world_radius(ship))
    T = LOOK_AHEAD_S
    C = (p[0] + v[0] * T + 0.5 * a[0] * T * T,
         p[1] + v[1] * T + 0.5 * a[1] * T * T,
         p[2] + v[2] * T + 0.5 * a[2] * T * T)
    D = _len(_sub(C, p))
    R = ship_r * personal_space_factor(_len(v))
    Q = max(D + R, MINIMUM_QUERY_RADIUS_GU)

    try:
        ship_disabled = _collision_disabled_ids(ship)
        ship_id = ship.GetObjID()
    except Exception:
        ship_disabled, ship_id = frozenset(), None

    now = _now()
    memory = node.__dict__.setdefault(_MEMORY_ATTR, {})
    listed = []                       # (o, w, r)
    listed_ids = set()
    in_set = {}
    for (other, ox, oy, oz, r, _vel, _gate_r, ob_id,
         ob_disabled) in obstacle_snapshot(pSet):
        if other is ship:
            continue
        in_set[id(other)] = other
        if ob_id in ship_disabled or (ship_id is not None and ship_id in ob_disabled):
            continue                  # Dauntless policy: collisions disabled
        o = (ox, oy, oz)
        if _len(_sub(o, C)) - r > Q:
            continue
        if _is_cloaked(other):
            continue
        w, b = _physics_motion(other)
        if must_avoid(p, v, a, R, o, w, b, r):
            listed.append((o, w, r))
            listed_ids.add(id(other))
            memory[id(other)] = (other, now)

    # Memory: anything stamped within the last second and still in the set
    # holds the avoidance, at its current position and velocity, untested.
    for key, (obj, stamp) in list(memory.items()):
        if now - stamp > MEMORY_S or key not in in_set:
            del memory[key]
            continue
        if key in listed_ids:
            continue
        w, _b = _physics_motion(obj)
        listed.append((_v(obj.GetWorldLocation()), w, float(world_radius(obj))))

    # Dauntless policy: large rock-field rocks as static obstacles.
    try:
        from engine.rocks import field_obstacles
        from engine.rocks import far_dials as _fd
        zero = (0.0, 0.0, 0.0)
        for fx, fy, fz, fr in field_obstacles.near(
                pSet, C, float(_fd.get("avoid_query_radius_gu"))):
            if must_avoid(p, v, a, R, (fx, fy, fz), zero, zero, fr):
                listed.append(((fx, fy, fz), zero, fr))
    except Exception:
        pass

    if not listed:
        return None, None
    forward = _v(ship.GetWorldForwardTG())
    infos = [_obstacle_info(p, ship_r, o, w, r, forward) for o, w, r in listed]
    return choose_flee(forward, infos)


def _is_player(ship) -> bool:
    try:
        return ship is App.Game_GetCurrentPlayer()
    except Exception:
        return False


class CompiledAvoidObstaclesMixin:
    """The engine AvoidObstacles node; mixed in ahead of the SDK class."""

    def GetNextUpdateTime(self):
        return 0.0

    def Update(self, dEndTime=None):
        state = self.__dict__
        code_ai = state.get("pCodeAI")
        ship = code_ai.GetShip() if code_ai is not None else None
        if ship is None:
            return App.PreprocessingAI.PS_NORMAL
        try:
            warping = bool(ship.IsDoingInSystemWarp())
        except Exception:
            warping = False
        engines = ship.GetImpulseEngineSubsystem()
        if warping or engines is None or engines.IsDisabled():
            return App.PreprocessingAI.PS_NORMAL

        direction, speed = evaluate_course(self, ship)
        # The SDK node's own override fields, kept current so anything that
        # observes an AvoidObstacles node (the AI inspector, tests) still can.
        # Observability only: the node never reads them back.
        state["vOverrideDirection"] = TGPoint3(*direction) if direction is not None else None
        state["fOverrideSpeed"] = speed
        if direction is not None:
            ship.TurnTowardDirection(TGPoint3(*direction))
            from engine.appc.objects import PhysicsObjectClass
            ship.SetImpulse(speed, TGPoint3(0.0, 1.0, 0.0),
                            PhysicsObjectClass.DIRECTION_MODEL_SPACE)
            if not _is_player(ship):
                if state.get(_SAVED_POWER_ATTR) is None:
                    state[_SAVED_POWER_ATTR] = engines.GetPowerPercentageWanted()
                engines.SetPowerPercentageWanted(AVOID_IMPULSE_POWER)
            return App.PreprocessingAI.PS_SKIP_ACTIVE

        saved = state.get(_SAVED_POWER_ATTR)
        if saved is not None:
            engines.SetPowerPercentageWanted(saved)
            state[_SAVED_POWER_ATTR] = None
        return App.PreprocessingAI.PS_NORMAL
