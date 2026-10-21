"""Engine-side replacements for the Python preprocessors the original engine
compiled into C++ ("CodeAI"), and the registry that swaps them in.

Mechanism, from the binary (2026-07-14): ``PreprocessingAI::SetContainedAI``
(0x0048E570) does not store the AI it is handed — it calls
``newAI->GetOptimizedVersion()`` (vtable +0x34) and stores the RETURNED object.
``PreprocessingAI`` overrides that slot (0x0048EB20): it reads the bound Python
preprocessor's class name, looks it up in a native registry (DAT_00982A1C), and
on a hit allocates a native node, steals the contained subtree, and deletes the
Python-backed node outright. The BaseAI default (0x00470750) is
``MOV EAX,ECX; RET`` — return ``this``, i.e. "I have no optimized version,
use me".

Four classes are registered in the binary: ``AvoidObstacles``, ``FireScript``,
``ManagePower``, ``SelectTarget``. In the shipped game all four Python ``Update``
bodies are DEAD CODE — the native nodes replace them at bind time. We have no
native versions, so we run those Python bodies. Three of the four therefore need
an entry here, for two different reasons:

* ``ManagePower`` gets a REPLACEMENT (the class below), which also runs the
  engine's power budget and split. Its SDK Python body
  (``AI/Preprocessors.py:2148``) is ``# Unused.  return PS_DONE`` — an explicit
  stub. PS_DONE maps to US_DONE, which DESTROYS the AI node. It sits in the live
  FedAttack / NonFedAttack / CloakAttack chain (AlertLevel -> PowerManagement ->
  FleeAttackOrFollow), so running the Python body would delete every Federation
  ship's AI within one 3-second cadence. The ``SetInterruptable(1)`` bypass
  cannot save it: ``FleeAttackOrFollow`` sets that flag itself.
* ``FireScript`` and ``AvoidObstacles`` get NON-LETHAL WRAPPERS. Their SDK Python
  bodies are full working implementations that we NEED and run verbatim — but
  each has one edge that returns PS_DONE:

      FireScript.Update (:284)     `pTarget = self.GetTarget()`
                                   `if not pTarget: return PS_DONE`
      AvoidObstacles.Update (:1688) `if pShip == None: return PS_DONE`

  Those edges are reachable from live doctrine (every Attack doctrine builds a
  FireScript node; targets die and leave sets), and US_DONE is unrecoverable in
  our driver — ``_tick_priority_list`` skips US_DONE children forever. So the
  wrapper delegates every call to the real SDK body and translates ONLY a
  PS_DONE return into PS_NORMAL.

  *** KNOWN DIVERGENCE — READ THIS BEFORE TRUSTING IT. ***
  We do NOT know what the native FireScript / AvoidObstacles return when they
  have no target / no ship. That remains an OPEN QUESTION for the RE project.
  The one thing we know for certain is that it is NOT lethal: a Federation ship
  in the shipped game that loses its target does not lose its AI — it re-acquires
  and keeps fighting.

  THE CHOSEN VALUE IS PS_NORMAL ("run the contained AI"), for two reasons:

  1. It is exactly what shipped before this branch. The old driver mapped PS_DONE
     to "stop calling the preprocessor, fall through and dispatch the child", so
     the node stayed US_ACTIVE and its subtree kept running. PS_NORMAL reproduces
     that behaviour and adds nothing — it is the minimum change that removes the
     lethality. (It is even strictly better: the old mapping *latched* the
     preprocessor off forever, so a FireScript that once lost its target never
     fired again; PS_NORMAL lets it re-run on its next cadence and re-acquire.)
  2. PS_SKIP_DORMANT (the earlier guess) maps to US_DORMANT, and US_DORMANT is a
     ONE-WAY TRAP in our driver: ``_tick_priority_list`` skips US_DORMANT children
     forever (they are never re-dispatched, so they can never leave dormancy), and
     a ``SequenceAI`` with ``skip_dormant = 0`` HOLDS on a dormant child. An
     ``AvoidObstacles`` node is a direct ``SequenceAI`` child in 7+ shipped trees
     (AI/Compound/TractorDockTargets.py:213, Maelstrom/Episode1/E1M1/UndockAI.py:63,
     E1M1_AI_Devore.py:47, E2M0_AI_Warbird.py:260, E2M1_AI_WarbirdTow.py:94,
     E4M6/CenterFieldAI.py:43), so a momentary no-ship tick would permanently wedge
     the sequence. Trading one unrecoverable state for another is no fix.

  The nearest evidence FOR a dormant return is that the SDK's own SelectTarget
  reports its no-target state as PS_SKIP_DORMANT (``eNoTargetPreprocessStatus``) —
  but that is a different class, and it is not worth buying a new wedge with. Read
  PS_NORMAL as "the pre-branch behaviour, minus the lethality", not as faithfulness.

* ``SelectTarget`` is deliberately NOT registered, even though the binary
  replaces it. Its Python body cannot return PS_DONE at all (its no-target path
  returns PS_SKIP_DORMANT), so running it cannot kill a node — it needs no
  protection. Pinned by
  ``tests/unit/test_preprocess_done_is_lethal.py::test_select_targets_sdk_body_cannot_return_ps_done``.

``AlertLevel`` is deliberately absent from both registries — it is not in the
binary's either, which is exactly why *its* Python body correctly returns
PS_NORMAL.
"""

import functools
import os

import App


# ── ManagePower (cleanroom/spec/03_ManagePower_compiled.md) ─────────────────
MANAGE_POWER_INTERVAL_S = 3.0
# Battery threshold T as a fraction of the main-battery limit L.
MANAGE_POWER_THRESHOLD = 0.9
MANAGE_POWER_CONSERVE_THRESHOLD = 1.0625
# Below the threshold the budget falls linearly from the plant's output toward
# this fraction of it (reached on an empty battery).
MANAGE_POWER_EMPTY_FRACTION = 0.1
# No system is asked for more than this multiple of its normal demand.
MANAGE_POWER_MAX_LEVEL = 1.25
# Split weights; every other powered system weighs 1.
MANAGE_POWER_SENSOR_WEIGHT = 0.25
MANAGE_POWER_WARP_WEIGHT = 0.05


def manage_power_budget(power, conserve) -> float:
    """The power budget the AI splits among its ship's systems.

    A = main battery power, L = its limit, P = plant output (already scaled by
    the plant's condition), S = power available through the conduits:
    min(A, main conduit capacity) + min(backup battery, backup conduit
    capacity) -- which is what our battery-limited conduit getters return.

    L <= 0 -> P. Otherwise T = 0.9 L (1.0625 L conserving), f = A / 2T below
    T, else 1 - (L - A) / 2(L - T); f < 0.5 -> P*2f + 0.1P*(1 - 2f) (below the
    output), else P*(2 - 2f) + (2f - 1)*S (the output at T, rising to S on a
    full battery).
    """
    P = float(power.GetPowerOutput())
    L = float(power.GetMainBatteryLimit())
    if L <= 0.0:
        return P
    A = float(power.GetMainBatteryPower())
    S = float(power.GetMainConduitCapacity()) + float(power.GetBackupConduitCapacity())
    T = L * (MANAGE_POWER_CONSERVE_THRESHOLD if conserve else MANAGE_POWER_THRESHOLD)
    if A < T:
        f = 0.5 * A / T
    else:
        f = 1.0 - 0.5 * (L - A) / (L - T)
    if f < 0.5:
        return P * 2.0 * f + MANAGE_POWER_EMPTY_FRACTION * P * (1.0 - 2.0 * f)
    return P * (2.0 - 2.0 * f) + (2.0 * f - 1.0) * S


def _manage_power_weight(system) -> float:
    from engine.appc.subsystems import SensorSubsystem, WarpEngineSubsystem
    if isinstance(system, SensorSubsystem):
        return MANAGE_POWER_SENSOR_WEIGHT
    if isinstance(system, WarpEngineSubsystem):
        return MANAGE_POWER_WARP_WEIGHT
    return 1.0


def _manage_power_demand(system) -> float:
    """Normal demand: 0 when disabled or off, 0 for a tractor not firing,
    else the system's normal power per second."""
    from engine.appc.weapon_subsystems import TractorBeamSystem
    if system.IsDisabled() or not system.IsOn():
        return 0.0
    if isinstance(system, TractorBeamSystem) and not system.IsFiring():
        return 0.0
    return float(system.GetNormalPowerPerSecond())


def manage_power_systems(ship) -> list:
    """Every powered system of `ship` taking part in the split: all but the
    power plant (not a consumer) and the cloaking device (never touched)."""
    from engine.appc.subsystems import CloakingSubsystem, PowerSubsystem
    consumers = getattr(ship, "__dict__", {}).get("_powered_consumers") or ()
    return [s for s in consumers
            if not isinstance(s, (CloakingSubsystem, PowerSubsystem))]


def manage_power_split(systems, budget) -> list:
    """Split `budget` among `systems` by weight; returns [(system, level)].

    Each pass gives every remaining system weight / W of the budget, as a
    level of its normal demand (0 for no demand). Any level above 1.25 is set
    to 1.25 and leaves the split, taking 1.25 x its demand off the budget and
    its weight off W; the rest take their level. Repeat while anything was
    capped. A system that wants no power keeps its weight: its share is lost.
    """
    entries = [(s, _manage_power_weight(s), _manage_power_demand(s)) for s in systems]
    W = sum(w for _s, w, _d in entries)
    levels: dict = {}
    remaining = entries
    while remaining and W > 0.0:
        capped, kept = [], []
        for s, w, d in remaining:
            level = (w / W * budget) / d if d > 0.0 else 0.0
            if level > MANAGE_POWER_MAX_LEVEL:
                capped.append((s, w, d))
            else:
                kept.append((s, w, d))
                levels[id(s)] = (s, level)
        for s, w, d in capped:
            levels[id(s)] = (s, MANAGE_POWER_MAX_LEVEL)
            budget -= MANAGE_POWER_MAX_LEVEL * d
            W -= w
        if not capped:
            break
        remaining = kept
    return list(levels.values())


class ManagePower:
    """The engine's ManagePower node, replacing the SDK's stub.

    Every 3.0 s it sets the wanted power level of each powered system on its
    ship (manage_power_budget, manage_power_split), reading ``bConservePower``
    off the Python instance it replaced (CloakAttack passes 1, FedAttack and
    NonFedAttack 0). It always returns PS_NORMAL ("run the contained AI"):
    NEVER PS_DONE, which would delete the whole combat subtree.
    """

    def __init__(self, bConservePower=0):
        self.bConservePower = bConservePower

    def GetNextUpdateTime(self):
        return MANAGE_POWER_INTERVAL_S

    def _ship(self):
        ai = self.__dict__.get("pCodeAI")
        get_ship = getattr(ai, "GetShip", None) if ai is not None else None
        return get_ship() if callable(get_ship) else None

    def Update(self, dEndTime=None):
        ship = self._ship()
        power = ship.GetPowerSubsystem() if ship is not None else None
        if power is not None:
            budget = manage_power_budget(power, bool(self.bConservePower))
            for system, level in manage_power_split(
                    manage_power_systems(ship), budget):
                system.SetPowerPercentageWanted(level)
        # PS_NORMAL: run the contained AI. NEVER PS_DONE — that is lethal.
        return App.PreprocessingAI.PS_NORMAL


# Re-scan cadence for a ship that is ACTIVELY EVADING.
#
# AvoidObstacles.__init__ ships:
#     self.fMinimumUpdateDelay = 0.0  # 0.25
#     self.fMaximumUpdateDelay = 0.25 # 1.25
# The commented values are BC's earlier, slower tuning; the live ones are what
# they turned it down to. They could afford it -- AvoidObstacles is one of the
# four preprocessors GetOptimizedVersion swaps for a NATIVE node, so in the
# shipped game a 60 Hz re-scan is C++. Ours is the Python scan, and at 100
# ships in a 65 km sphere ~44 of them are evading at any moment, each
# re-scanning every tick: 6.5 ms of an 18.5 ms tick, 36% of the whole
# gameloop, spent recomputing an answer that changes negligibly in 16 ms.
#
# Restoring their commented 0.25 is safe because a cadence-skipped tick does
# NOT drop the evasion: _tick_preprocessing's else-branch reproduces the last
# PS_SKIP_ACTIVE, so the contained AI stays suppressed and the ship holds the
# TurnTowardDirection / SetImpulse setpoints already issued. The ship keeps
# evading on the committed heading; only the re-decision waits. Against a 15 s
# fPredictionTime lookahead, a 0.25 s stale decision is 1.7% of the horizon.
#
# Set to 0.0 to restore the SDK's every-tick behaviour exactly.
AVOID_EVADING_UPDATE_DELAY_S = float(
    os.environ.get("DAUNTLESS_AVOID_EVADE_DELAY_S", "0.25"))

# ── First-schedule phase spread (the thundering herd) ───────────────────────
#
# With fMinimumUpdateDelay == fMaximumUpdateDelay == 0.25 and the driver
# rescheduling as `game_time + interval` (ai_driver._tick_preprocessing), the
# cadence is a pure PERIOD with no phase: two nodes that are ever due on the
# same tick are due on the same tick for the rest of the mission, and ships
# spawned in the same frame start that way. Measured at 8 ships, scans per tick
# was [8,0,0,...,0,8,0,...] -- the MEAN dropped 15x but the per-tick PEAK did
# not move, so the frame-time spike the cadence was meant to flatten survived.
#
# One offset applied to the FIRST reschedule breaks the lock permanently: after
# it the node runs at exactly BC's rate, merely out of step with its
# neighbours. It is a phase shift, not a rate change.
#
# Deterministic by construction -- NO `random` in sim code. The offset is a
# function of the ship's object id, so the same ship gets the same phase on
# every run and across a save/restore. See _phase_factor for why the id is
# avalanche-mixed rather than taken modulo directly.
#
# The offset only ever SHORTENS the first interval (factor in (0, 1]), so no
# re-decision is ever pushed past BC's own fMaximumUpdateDelay. Avoidance is a
# safety system; the acceptable direction to err in is "scans sooner".
#
# 16 buckets: the interval is 0.25 s and a tick is 1/60 s, so the schedule has
# 15 distinguishable tick phases. More buckets than that buys nothing (two
# offsets under a tick apart land on the same tick); fewer wastes phases.
_PHASE_BUCKETS = 16
_PHASE_ATTR = "_avoid_phase_applied"

# Fallback ordinal for a node whose ship is not resolvable yet (pCodeAI is
# bound after construction, and the driver can reach GetNextUpdateTime first).
# Deterministic given deterministic construction order, which is what a mission
# script provides.
_PHASE_FALLBACK = [0]


def _phase_key(node) -> int:
    """A stable small integer identifying `node`'s ship, for phase bucketing."""
    # __dict__, not getattr: an engine-backed node inherits TGObject.__getattr__
    # and would hand back a truthy _Stub for a missing pCodeAI.
    ai = node.__dict__.get("pCodeAI")
    get_ship = getattr(ai, "GetShip", None) if ai is not None else None
    if callable(get_ship):
        try:
            ship = get_ship()
        except Exception:
            ship = None
        if ship is not None:
            try:
                obj_id = ship.GetObjID()
            except Exception:
                obj_id = None
            # A _Stub floats/ints to 0 and would put every ship in one bucket,
            # which is the state this exists to leave. Demand a real integer.
            if isinstance(obj_id, int) and not isinstance(obj_id, bool):
                return abs(obj_id)
    _PHASE_FALLBACK[0] += 1
    return _PHASE_FALLBACK[0]


def _phase_factor(node) -> float:
    """Fraction of the nominal interval this node's FIRST reschedule waits.

    In ``(0, 1]`` -- never longer than the cadence it is offsetting.

    A bare ``obj_id % _PHASE_BUCKETS`` is NOT sufficient and was the first
    version of this. Object ids come off one global counter shared with every
    subsystem, hardpoint and property a ship allocates, so consecutive SHIPS
    are strided, not consecutive -- and any stride that is a multiple of the
    bucket count (which a fixed per-ship allocation count easily is) maps every
    ship to the same bucket, restoring the exact herd this removes.

    So the id goes through a full avalanche (multiply / xor-shift / multiply /
    xor-shift) before the modulo, and NOT through "one multiply, then read some
    hand-picked bit window" -- which was the second version and was worse than
    useless: bits 20-23 of ``id * 2654435761`` barely move across small strided
    ids, so an 8-ship crowd collapsed onto 3 tick phases and the measured peak
    went UP. Avalanche first, then take the low bits; measured 5-8 distinct
    phases out of 8 ships at every stride tried (1, 8, 16, 37, 40, 64, 128,
    256).
    """
    h = (_phase_key(node) * 2654435761) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    return float((h % _PHASE_BUCKETS) + 1) / _PHASE_BUCKETS


_ENGINE_AVOIDANCE_CLASSES: dict = {}


def _engine_avoidance_class(base: type) -> type:
    """Subclass of the non-lethal wrapper whose ``TestCourseOverride`` is ours.

    A subclass of the NON-LETHAL class, not of the SDK class directly, so the
    PS_DONE de-fanging still applies -- AvoidObstacles.Update returns PS_DONE
    when it has no ship, and US_DONE is unrecoverable in our driver.

    TWO methods are ours and no more: ``TestCourseOverride`` (the world scan --
    the only slow part) and ``GetNextUpdateTime``, which adds the one-off
    first-schedule phase offset described above and is otherwise the SDK value.
    Everything else about the node stays SDK code operating on SDK state: the
    PS_SKIP_ACTIVE return, TurnTowardDirection / SetImpulse, the ``fUpdateDelay``
    the SDK ``Update`` writes, and the pickle hooks. The alias shares the
    original's ``__dict__``, so every parameter the SDK ctor set stays live.

    ⚠️ WHAT IS *NOT* PRESERVED -- the engine scan reads MODULE CONSTANTS, not
    this node's fields. ``course_override_for`` passes only the node (for its
    ship and its ``vOverrideDirection``); the scan then uses
    ``collision_avoidance``'s ``AVOID_PREDICTION_TIME_S``,
    ``AVOID_MINIMUM_RADIUS_GU``, ``AVOID_PERSONAL_SPACE_MULT`` and the
    module-level ``_dont_avoid_types()`` in place of this instance's
    ``fPredictionTime``, ``fMinimumRadius``, ``fPersonalSpace`` and
    ``lDontAvoidTypes``. The fields are still there and still read by the SDK
    ``Update``; they simply no longer steer the scan.

    That is INERT for shipped content -- no stock SDK script customises any of
    the four, and ``tests/unit/test_avoid_obstacles_engine_node.py`` pins each
    against the real ``AI.Preprocessors.AvoidObstacles`` defaults -- but it is a
    real divergence, and a mod that varied any of them per-doctrine would be
    silently ignored. Closing it means threading the four off the node in
    ``course_override_for``; see docs/engine/avoidance-duplication.md.
    """
    cached = _ENGINE_AVOIDANCE_CLASSES.get(base)
    if cached is not None:
        return cached

    def TestCourseOverride(self):
        from engine.appc.collision_avoidance import course_override_for
        return course_override_for(self)

    cls = type("Engine" + base.__name__, (_phased_avoidance_class(base),),
               {"TestCourseOverride": TestCourseOverride})
    _ENGINE_AVOIDANCE_CLASSES[base] = cls
    globals()[cls.__name__] = cls
    return cls


_PHASED_AVOIDANCE_CLASSES: dict = {}


def _phased_avoidance_class(base: type) -> type:
    """Non-lethal `base` plus the one-off first-schedule phase offset.

    Split out of _engine_avoidance_class so the SDK scan gets it too. The
    offset and the evading cadence below were only ever applied to the ENGINE
    scan, which is opt-in — so on the path that actually runs, every ship
    re-scanned in lock-step and an evading ship re-scanned every tick. That went
    unnoticed only because the scan was inert (ProximityManager.GetNextObject
    was a hardcoded `return None`), so no ship ever reached the evading branch.
    """
    cached = _PHASED_AVOIDANCE_CLASSES.get(base)
    if cached is not None:
        return cached

    base_next_update = getattr(base, "GetNextUpdateTime", None)

    def GetNextUpdateTime(self):
        delay = base_next_update(self) if base_next_update is not None else 0.0
        state = self.__dict__
        if state.get(_PHASE_ATTR):
            return delay                       # SDK cadence, untouched
        state[_PHASE_ATTR] = True
        return delay * _phase_factor(self)

    cls = type("Phased" + base.__name__, (_non_lethal_class(base),),
               {"GetNextUpdateTime": GetNextUpdateTime})
    _PHASED_AVOIDANCE_CLASSES[base] = cls
    globals()[cls.__name__] = cls
    return cls


def _apply_evading_cadence(alias):
    """Restore BC's own commented-out `fMinimumUpdateDelay` (0.25).

    `AI/Preprocessors.py:1624` reads `self.fMinimumUpdateDelay = 0.0 # 0.25`.
    At 0.0 a ship that is evading re-runs the whole world scan EVERY tick, and
    in a crowded fight most ships are evading most of the time: measured at 32
    ships, ~14 scans per tick against the ~8 the 4 Hz cadence predicts, for
    +3.4 ms of sim per tick. A cadence-skipped tick does not drop the evasion —
    ai_driver._tick_preprocessing reproduces the last PS_SKIP_ACTIVE — so the
    ship holds its committed heading and only the re-decision waits.
    """
    if AVOID_EVADING_UPDATE_DELAY_S > 0.0:
        alias.__dict__["fMinimumUpdateDelay"] = AVOID_EVADING_UPDATE_DELAY_S
    return alias


def _wrap_avoid_obstacles(instance):
    """The DEFAULT AvoidObstacles binding: SDK scan, PS_DONE de-fanged, plus
    the phase offset and evading cadence."""
    cls = _phased_avoidance_class(type(instance))
    alias = cls.__new__(cls)
    alias.__dict__ = instance.__dict__
    return _apply_evading_cadence(alias)


def _replace_avoid_obstacles(instance):
    """Swap the SDK's AvoidObstacles scan for the engine-side one.

    Shares the original's ``__dict__`` (as _wrap_non_lethal does), so every
    parameter the SDK ctor set -- fPredictionTime, fMinimumRadius,
    fPersonalSpace, the manoeuvre angles, lDontAvoidTypes -- and every field
    Update mutates stay visible and live.

    "Visible and live" is not "consulted by the scan": see
    _engine_avoidance_class for which of them the engine scan ignores.
    """
    cls = _engine_avoidance_class(type(instance))
    alias = cls.__new__(cls)
    alias.__dict__ = instance.__dict__
    return _apply_evading_cadence(alias)


def _replace_manage_power(instance):
    """Swap the SDK's ManagePower stub for the engine-side class above."""
    # Carry the ctor arg across the swap, as the native ctor does by reading
    # bConservePower off the Python instance. __dict__ lookup, not getattr:
    # TGObject.__getattr__ hands back a truthy _Stub for missing attrs, so
    # getattr's default would never fire on an engine-backed instance.
    params = getattr(instance, "__dict__", {})
    return ManagePower(params.get("bConservePower", 0))


_NON_LETHAL_CLASSES: dict = {}


def _non_lethal_class(base: type) -> type:
    """Build (once per SDK class) a subclass whose only difference is that a
    PS_DONE return from the SDK's ``Update`` becomes PS_NORMAL.

    A subclass, not a delegating proxy, because the driver and the SDK bodies
    duck-type all over the instance (``inst.__dict__["idTargetedSubsystem"]``,
    ``lWeapons``, ``pCodeAI``, ``DamageEvent``, ``GotFocus``/``LostFocus``,
    ``CodeAISet``…). Inheritance forwards every one of them to the real SDK code
    with zero surface to keep in sync.
    """
    cached = _NON_LETHAL_CLASSES.get(base)
    if cached is not None:
        return cached

    # functools.wraps, so the driver's inspect.signature() arity probe
    # (ai_driver._tick_preprocessing) still sees the SDK's (self, dEndTime) and
    # passes the end time through.
    @functools.wraps(base.Update)
    def Update(self, *args, **kwargs):
        result = base.Update(self, *args, **kwargs)
        if result == App.PreprocessingAI.PS_DONE:
            # *** THE DIVERGENCE. *** The shipped engine never ran this Python
            # body — it ran a native class whose no-target/no-ship return value
            # we do NOT know (still an open question for the RE project). We know
            # only that it was NOT lethal: BC ships that lose a target keep their
            # AI and re-acquire.
            #
            # PS_NORMAL = "run the contained AI". That is EXACTLY what the driver
            # did with PS_DONE before this branch, so this reproduces the
            # shipping behaviour minus the lethality and introduces no new state.
            # PS_SKIP_DORMANT was considered and rejected: US_DORMANT is a one-way
            # trap in our driver (_tick_priority_list skips dormant children
            # forever; a skip_dormant=0 SequenceAI — which is how 7+ shipped trees
            # parent AvoidObstacles — HOLDS on one). See the module docstring.
            return App.PreprocessingAI.PS_NORMAL
        return result

    def GetChildTargets(self, pSubsystem):
        """Targetable, still-alive descendants of a non-targetable subsystem.

        Called from the SDK's ChooseTargetSubsystem (Preprocessors.py:832)
        and defined nowhere in the SDK — it belonged to BC's NATIVE
        FireScript, which is unreconstructed. Stated assumption: the only
        reading under which the SDK loop makes sense. Every stock hardpoint
        marks its aggregator systems SetTargetable(0) and their banks/tubes
        targetable (galaxy.py:774-995), so this branch is the common case for
        any explicit TargetSubsystems list (Fleet/DisableTarget, Player/
        Disable*). Without it: AttributeError out of the AI tick.
        """
        out = []
        n = int(pSubsystem.GetNumChildSubsystems()) if hasattr(pSubsystem, "GetNumChildSubsystems") else 0
        for i in range(n):
            child = pSubsystem.GetChildSubsystem(i)
            if child is None:
                continue
            if child.IsTargetable() and child.GetCondition() > 0:
                out.append(child)
            else:
                out.extend(GetChildTargets(self, child))
        return out

    members = {
        "Update": Update,
        "__doc__": (
            "SDK %s with its lethal PS_DONE return translated to "
            "PS_NORMAL. See engine/appc/ai_optimized.py." % base.__name__
        ),
    }
    if base.__name__ == "FireScript":
        members["GetChildTargets"] = GetChildTargets
    bases = (base,)
    if base.__name__ == "FireScript":
        # BC's binary swaps FireScript for its native OptimizedFireScript
        # (App.py:5186, a PreprocessingAI subclass carrying the control
        # surface). TacticalMenuHandlers.GetPlayerFiringAIScripts:1861 finds
        # the player's fire nodes by isinstance against that class — so the
        # wrapper must BE one. The SDK FireScript already defines every
        # method the binding lists (Preprocessors.py:172-230).
        from engine.appc.ai import OptimizedFireScript
        bases = (base, OptimizedFireScript)
    cls = type(base.__name__ + "_NonLethal", bases, members)
    _NON_LETHAL_CLASSES[base] = cls
    # Register the dynamic class in the module globals so pickle can find it at
    # unpickle time via attribute lookup. Repeated calls are idempotent due to
    # the cache check above (we only create one class per base type).
    globals()[cls.__name__] = cls
    return cls


def _wrap_non_lethal(instance):
    """Return a non-lethal alias of ``instance``.

    The alias shares the original's ``__dict__`` (same state object, no copy),
    so post-bind mutation by SDK callers — ``AddWeaponSystem``, ``SetTarget``,
    the subsystem-choice bookkeeping — is visible through both, and the driver's
    ``inst.__dict__`` probes keep working. The original is then discarded, as the
    shipped engine discards the Python-backed node it replaces.
    """
    cls = _non_lethal_class(type(instance))
    alias = cls.__new__(cls)
    alias.__dict__ = instance.__dict__
    return alias


# Python preprocessor class NAME -> factory(original_instance) -> the object the
# engine actually stores. Mirrors the binary's DAT_00982A1C name registry; see
# the module docstring for why three of its four entries are here and what kind
# of object each one yields.
OPTIMIZED_PREPROCESSORS: dict = {
    "ManagePower": _replace_manage_power,     # real replacement (SDK body is a stub)
    "FireScript": _wrap_non_lethal,           # SDK body, PS_DONE de-fanged
    # SDK scan, PS_DONE de-fanged, plus the phase offset and evading cadence.
    # This entry has always done TWO jobs and only one of them was the problem:
    # the engine scan (removed below, opt-in) and protection against
    # AvoidObstacles' lethal PS_DONE path, which a shipless node hits. Dropping
    # the whole entry would have taken the protection with it --
    # test_preprocess_done_is_lethal caught exactly that.
    "AvoidObstacles": _wrap_avoid_obstacles,
}

# ── AvoidObstacles: OFF by default, and why ────────────────────────────────
#
# The engine-side scan is opt-in via DAUNTLESS_ENGINE_AVOIDANCE=1. Default is
# the SDK's own AvoidObstacles.
#
# It shipped enabled and caused a live gameplay regression: 3 Keldons vs the
# player in QuickBattle flew around, fired an opening volley, then stopped
# engaging, with shields visibly dropping and raising. The chain:
#
#   our scan calls "evading" far more often than the SDK's -- 47.7% of pairs
#   pass the convergence gate while 1.48% are genuinely converging
#     -> Update returns PS_SKIP_ACTIVE
#     -> _tick_preprocessing honours that by returning WITHOUT ticking the
#        contained AI, so the whole attack subtree goes unreached
#     -> _reconcile_focus dispatches LostFocus across it
#     -> AlertLevel.LostFocus restores the pre-combat alert level (SHIELDS DOWN)
#        and FireScript.LostFocus calls StopFiring() (FIRING STOPS)
#
# Measured, 100 ships, combat_stress, median tick:
#
#   engine scan, as shipped        2.78 ms   <- attack subtree suppressed
#   engine scan, not suppressing   4.13 ms
#   SDK AvoidObstacles             2.91 ms
#
# So essentially the whole measured win was the suppression, not a cheaper
# scan: stop it switching combat off and the engine scan is ~40% SLOWER than
# the SDK's. Reverting costs ~5% of a median tick, and that 5% is the price of
# ships actually fighting -- payable under any correct fix.
#
# The code is kept, not deleted, because the *idea* is sound and the follow-up
# is well-defined: make the scan genuinely cheaper, and measure it against a
# NON-SUPPRESSING baseline (the 4.13 ms row) rather than against the shipped
# number, which measures the bug.
#
# tests/integration/test_avoidance_does_not_suppress_combat.py holds the line.
if os.environ.get("DAUNTLESS_ENGINE_AVOIDANCE") == "1":
    # Swaps the non-lethal wrapper above for the engine scan, which is itself
    # non-lethal (_engine_avoidance_class maps PS_DONE -> PS_NORMAL too).
    OPTIMIZED_PREPROCESSORS["AvoidObstacles"] = _replace_avoid_obstacles


def optimized_version_of(instance):
    """Appc's ``GetOptimizedVersion``, dispatched by class name.

    Returns the engine-side object on a registry hit, or the original instance
    unchanged otherwise — matching the C++ default, which returns ``this``.
    """
    if instance is None:
        return instance
    factory = OPTIMIZED_PREPROCESSORS.get(type(instance).__name__)
    if factory is None:
        return instance
    return factory(instance)
