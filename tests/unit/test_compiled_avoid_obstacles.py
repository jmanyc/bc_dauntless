"""The compiled AvoidObstacles node (cleanroom/spec/02_AvoidObstacles_compiled.md).

Bound through the preprocessor registry as the default; reads none of the SDK
instance's settings; runs every time the tree reaches it.
"""
import math

import pytest

import App
from engine.appc import ai_optimized
from engine.appc import compiled_avoid_obstacles as cao
from engine.appc.ai import PlainAI_Create, PreprocessingAI_Create
from engine.appc.collision_avoidance import invalidate_obstacle_snapshot
from engine.appc.math import TGPoint3
from engine.appc.ships import ShipClass
from engine.appc.subsystems import HullSubsystem, ImpulseEngineSubsystem

ZERO = (0.0, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    App.g_kSetManager._sets.clear()
    invalidate_obstacle_snapshot()
    t = [0.0]
    monkeypatch.setattr(cao, "_now", lambda: t[0])
    yield t
    invalidate_obstacle_snapshot()
    App.g_kSetManager._sets.clear()


# ── Constants ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("speed,factor", [
    (0.0, 2.5), (0.1, 2.5), (1.05, 3.25), (2.0, 4.0), (9.0, 4.0)])
def test_personal_space_factor(speed, factor):
    assert cao.personal_space_factor(speed) == pytest.approx(factor)


@pytest.mark.parametrize("r,eff", [(10.0, 10.0), (25.0, 25.0), (100.0, 125.0),
                                   (400.0, 500.0), (800.0, 925.0)])
def test_effective_radius(r, eff):
    assert cao.effective_radius(r) == pytest.approx(eff)


def test_node_constants():
    assert (cao.LOOK_AHEAD_S, cao.STEP_S, cao.STEPS) == (10.0, 0.5, 20)
    assert cao.MINIMUM_QUERY_RADIUS_GU == 225.0
    assert cao.MEMORY_S == 1.0
    assert (cao.BLOCKED_FACTOR, cao.FORWARD_FACTOR) == (3.25, -0.25)
    assert cao.MOVING_SPEED == 0.0625
    assert cao.AVOID_IMPULSE_POWER == 1.25


# ── Must it be avoided? ──────────────────────────────────────────────────────

def test_already_within_personal_space_plus_effective_radius():
    # 140 apart; R 10; big obstacle r 100 -> effective 125: 140 < 135? no.
    assert not cao.must_avoid(ZERO, ZERO, ZERO, 10.0, (140.0, 0, 0), ZERO, ZERO, 100.0)
    # r 120 -> effective 150: 140 < 160.
    assert cao.must_avoid(ZERO, ZERO, ZERO, 10.0, (140.0, 0, 0), ZERO, ZERO, 120.0)


def test_obstacle_radius_does_not_enter_the_stepped_test():
    """Passing 12 GU abeam with personal space 5: avoided only if the radius
    counted, and it does not (outside the initial overlap check)."""
    v = (0.0, 10.0, 0.0)
    o = (12.0, 50.0, 0.0)
    assert not cao.must_avoid(ZERO, v, ZERO, 5.0, o, ZERO, ZERO, 10.0)
    assert cao.must_avoid(ZERO, v, ZERO, 13.0, o, ZERO, ZERO, 0.0)


def test_horizon_is_twenty_half_second_steps():
    v = (0.0, 10.0, 0.0)
    # 10 s at 10 GU/s covers 100 GU: within R = 1 of 100.5, not of 101.5.
    assert cao.must_avoid(ZERO, v, ZERO, 1.0, (0.0, 100.5, 0.0), ZERO, ZERO, 0.0)
    assert not cao.must_avoid(ZERO, v, ZERO, 1.0, (0.0, 101.5, 0.0), ZERO, ZERO, 0.0)


def test_crossing_inside_a_step_is_caught():
    """A fast obstacle crosses the ship's position between two sample times."""
    w = (100.0, 0.0, 0.0)
    o = (-25.0, 0.0, 0.0)              # at t=0.25 it is on the ship
    assert cao.must_avoid(ZERO, ZERO, ZERO, 1.0, o, w, ZERO, 0.0)


def test_acceleration_is_held_through_the_look_ahead():
    a = (0.0, 2.0, 0.0)                # 0.5 a t^2 = 100 at t = 10
    o = (0.0, 95.0, 0.0)
    assert cao.must_avoid(ZERO, ZERO, a, 1.0, o, ZERO, ZERO, 0.0)
    assert not cao.must_avoid(ZERO, ZERO, ZERO, 1.0, o, ZERO, ZERO, 0.0)


def test_diverging_does_not_need_avoiding():
    assert not cao.must_avoid(ZERO, (0, -5, 0), ZERO, 5.0, (0, 30, 0), (0, 5, 0), ZERO, 2.0)


# ── Flee direction ───────────────────────────────────────────────────────────

def test_cone_and_favourability():
    d, w, fav, cone = cao._obstacle_info(ZERO, 3.0, (0.0, 50.0, 0.0), ZERO, 2.0, (0, 1, 0))
    assert d == (0.0, 1.0, 0.0)
    assert fav == pytest.approx(-1.0)
    assert cone == pytest.approx(math.sqrt(50 ** 2 - 5 ** 2) / 50)
    _d, _w, _f, cone_in = cao._obstacle_info(ZERO, 3.0, (0.0, 4.0, 0.0), ZERO, 2.0, (0, 1, 0))
    assert cone_in == 0.0


def test_appeal_terms():
    fwd = (0.0, 1.0, 0.0)
    fav, cone = -2.0, 0.9
    static = [((0.0, 1.0, 0.0), ZERO, fav, cone)]
    # Into the cone: 3.25 * fav and nothing else.
    assert cao.appeal((0.0, 1.0, 0.0), fwd, static) == pytest.approx(3.25 * fav)
    # Static, outside the cone: 2 fav ((t.d + 1)/(cone + 1) - 0.5) - 0.25 fav (t.fwd).
    t = (1.0, 0.0, 0.0)
    assert cao.appeal(t, fwd, static) == pytest.approx(2 * fav * (1.0 / 1.9 - 0.5))
    t = (0.0, -1.0, 0.0)
    assert cao.appeal(t, fwd, static) == pytest.approx(2 * fav * (0.0 - 0.5) + 0.25 * fav)
    # Moving obstacle (velocity along +x), direction +y, t = +z:
    moving = [((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), fav, cone)]
    t = (0.0, 0.0, 1.0)
    # |cos(t, w)| = 0 -> 2 fav (0 - 0.5); c = (0,0,1).(0,1,0) = 0 -> 2 fav (0 - 0.5).
    assert cao.appeal(t, fwd, moving) == pytest.approx(2 * fav * -0.5 + 2 * fav * -0.5)
    t = (0.0, -1.0, 0.0)
    # c = -1 -> fav (-1 - (-1)) = 0; |cos| 0 -> -fav; forward term +0.25 fav.
    assert cao.appeal(t, fwd, moving) == pytest.approx(2 * fav * -0.5 + 0.0 + 0.25 * fav)


def test_slow_obstacle_is_treated_as_static():
    fwd = (0.0, 1.0, 0.0)
    slow = [((0.0, 1.0, 0.0), (0.06, 0.0, 0.0), -2.0, 0.9)]
    static = [((0.0, 1.0, 0.0), ZERO, -2.0, 0.9)]
    t = (1.0, 0.0, 0.0)
    assert cao.appeal(t, fwd, slow) == pytest.approx(cao.appeal(t, fwd, static))


def test_choose_flee_is_the_best_candidate_and_speed_rule():
    fwd = (0.0, 1.0, 0.0)
    ahead = cao._obstacle_info(ZERO, 3.0, (0.0, 20.0, 0.0), ZERO, 3.0, fwd)
    direction, speed = cao.choose_flee(fwd, [ahead])
    candidates = [(0.0, -1.0, 0.0)] + list(cao._LATTICE)
    best = max(cao.appeal(c, fwd, [ahead]) for c in candidates)
    assert cao.appeal(direction, fwd, [ahead]) == pytest.approx(best)
    assert speed == 0.0, "forward lies inside the obstacle's cone"
    beside = cao._obstacle_info(ZERO, 3.0, (20.0, 0.0, 0.0), ZERO, 3.0, fwd)
    assert cao.choose_flee(fwd, [beside])[1] == 1.0


def test_lattice_is_the_26_unit_cube_directions():
    assert len(cao._LATTICE) == 26
    assert all(abs(math.sqrt(sum(c * c for c in v)) - 1.0) < 1e-12 for v in cao._LATTICE)


def test_tie_goes_to_the_earliest_candidate():
    fwd = (0.0, 1.0, 0.0)
    direction, _ = cao.choose_flee(fwd, [])
    assert direction == cao._LATTICE[0]


# ── The node ─────────────────────────────────────────────────────────────────

def _scene():
    pSet = App.SetClass_Create()
    pSet.SetName("S")
    App.g_kSetManager._sets["S"] = pSet
    return pSet


def _ship(pSet, name, pos, vel=(0.0, 0.0, 0.0), radius=2.0):
    s = ShipClass()
    s.SetName(name)
    s._hull = HullSubsystem("H")
    s._hull.SetMaxCondition(1000.0)
    s.SetImpulseEngineSubsystem(ImpulseEngineSubsystem("IES"))
    s.SetRadius(radius)
    s.SetTranslateXYZ(*pos)
    s.SetVelocity(TGPoint3(*vel))
    pSet.AddObjectToSet(s, name)
    return s


def _node(ship):
    import AI.Preprocessors
    node = PreprocessingAI_Create(ship, "Avoid")
    node.SetContainedAI(PlainAI_Create(ship, "child"))
    node.SetPreprocessingMethod(AI.Preprocessors.AvoidObstacles(), "Update")
    return node.GetPreprocessingInstance()


def test_default_binding_is_the_compiled_node_and_sdk_is_selectable():
    pSet = _scene()
    ship = _ship(pSet, "A", (0, 0, 0))
    inst = _node(ship)
    assert isinstance(inst, cao.CompiledAvoidObstaclesMixin)
    assert inst.GetNextUpdateTime() == 0.0
    previous = ai_optimized.use_avoid_obstacles_binding("sdk")
    try:
        assert not isinstance(_node(ship), cao.CompiledAvoidObstaclesMixin)
    finally:
        ai_optimized.OPTIMIZED_PREPROCESSORS["AvoidObstacles"] = previous


def test_settings_are_ignored():
    pSet = _scene()
    ship = _ship(pSet, "A", (0, 0, 0), vel=(0, 5, 0))
    _ship(pSet, "B", (0, 120, 0))      # reached at ~t 24 s: beyond 10 s
    inst = _node(ship)
    inst.fPredictionTime = 60.0
    inst.fPersonalSpace = 50.0
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_no_ship_runs_the_contained_ai():
    import AI.Preprocessors
    node = PreprocessingAI_Create(None, "Avoid")
    node.SetPreprocessingMethod(AI.Preprocessors.AvoidObstacles(), "Update")
    assert node.GetPreprocessingInstance().Update(0.0) == App.PreprocessingAI.PS_NORMAL


def _head_on():
    pSet = _scene()
    ship = _ship(pSet, "A", (0, 0, 0), vel=(0, 5, 0))
    other = _ship(pSet, "B", (0, 40, 0), vel=(0, -5, 0))
    return pSet, ship, other


def test_avoiding_skips_contained_ai_and_raises_impulse_power():
    _pSet, ship, _other = _head_on()
    engines = ship.GetImpulseEngineSubsystem()
    engines.SetPowerPercentageWanted(0.8)
    inst = _node(ship)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_SKIP_ACTIVE
    assert engines.GetPowerPercentageWanted() == pytest.approx(1.25)
    assert inst.__dict__[cao._SAVED_POWER_ATTR] == pytest.approx(0.8)
    inst.Update(0.0)
    assert inst.__dict__[cao._SAVED_POWER_ATTR] == pytest.approx(0.8), "remembered once"


def test_power_restored_when_clear(_isolate):
    _pSet, ship, other = _head_on()
    engines = ship.GetImpulseEngineSubsystem()
    engines.SetPowerPercentageWanted(0.8)
    inst = _node(ship)
    inst.Update(0.0)
    other.SetTranslateXYZ(0, 5000, 0)
    other.SetVelocity(TGPoint3(0, 0, 0))
    invalidate_obstacle_snapshot()
    _isolate[0] = 2.0                   # past the 1 s memory
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL
    assert engines.GetPowerPercentageWanted() == pytest.approx(0.8)
    assert inst.__dict__[cao._SAVED_POWER_ATTR] is None


def test_player_power_is_never_touched(monkeypatch):
    _pSet, ship, _other = _head_on()
    engines = ship.GetImpulseEngineSubsystem()
    engines.SetPowerPercentageWanted(0.8)
    monkeypatch.setattr(cao, "_is_player", lambda s: s is ship)
    inst = _node(ship)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_SKIP_ACTIVE
    assert engines.GetPowerPercentageWanted() == pytest.approx(0.8)


@pytest.mark.parametrize("break_it", ["warp", "no_engines", "disabled"])
def test_warp_or_unusable_engines_run_the_contained_ai(monkeypatch, break_it):
    _pSet, ship, _other = _head_on()
    if break_it == "warp":
        monkeypatch.setattr(ship, "IsDoingInSystemWarp", lambda: 1)
    elif break_it == "no_engines":
        ship._impulse_engine_subsystem = None
    else:
        eng = ship.GetImpulseEngineSubsystem()
        eng.SetMaxCondition(100.0)
        eng.SetDisabledPercentage(0.5)
        eng.SetCondition(10.0)
    inst = _node(ship)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_memory_holds_a_detection_for_one_second(_isolate):
    _pSet, ship, other = _head_on()
    inst = _node(ship)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_SKIP_ACTIVE
    # Turn the threat away: no longer must-avoid, but remembered.
    other.SetTranslateXYZ(0, 200, 0)
    other.SetVelocity(TGPoint3(0, 5, 0))
    invalidate_obstacle_snapshot()
    _isolate[0] = 0.9
    assert inst.Update(0.0) == App.PreprocessingAI.PS_SKIP_ACTIVE
    invalidate_obstacle_snapshot()
    _isolate[0] = 1.2
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_memory_forgets_objects_that_left_the_set(_isolate):
    pSet, ship, other = _head_on()
    inst = _node(ship)
    inst.Update(0.0)
    pSet.RemoveObjectFromSet("B")
    invalidate_obstacle_snapshot()
    _isolate[0] = 0.5
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_cloaked_ships_are_not_obstacles(monkeypatch):
    _pSet, ship, other = _head_on()
    monkeypatch.setattr(other, "IsCloaked", lambda: 1)
    assert _node(ship).Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_collision_disabled_pair_is_never_an_obstacle():
    _pSet, ship, other = _head_on()
    ship.EnableCollisionsWith(other, 0)
    invalidate_obstacle_snapshot()
    assert _node(ship).Update(0.0) == App.PreprocessingAI.PS_NORMAL


def test_big_obstacle_counts_by_its_surface():
    """Query radius 225 GU around the predicted point; a 300 GU planet whose
    centre is 500 GU away still has its surface inside it."""
    pSet = _scene()
    ship = _ship(pSet, "A", (0, 0, 0), vel=(0, 1, 0))
    _ship(pSet, "Big", (0, 500, 0), radius=300.0)
    # Effective radius min(375, 425) = 375; path reaches within R of the
    # centre? No -- but the initial overlap test says 500 < R + 375 is false,
    # and in 10 s the ship covers 10 GU: nothing to avoid, yet it was queried.
    inst = _node(ship)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL
    ship.SetTranslateXYZ(0, 120, 0)
    invalidate_obstacle_snapshot()
    assert inst.Update(0.0) == App.PreprocessingAI.PS_SKIP_ACTIVE   # 380 < R + 375
