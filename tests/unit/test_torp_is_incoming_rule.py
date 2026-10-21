"""Which torpedoes count as incoming (cleanroom/spec/04_TorpIsIncoming.md).

For a torpedo, an observing ship, a danger threshold (s) and an optional source
filter:
  1. armed source filter -> the torpedo's firing object must be the given one;
  2. the torpedo is in the observing ship's set;
  3. its TARGET is the observing ship (aimed elsewhere, unguided, or the ship's
     own: never, however it flies);
  4. moving toward the ship: (torp pos - ship pos) . torp velocity <= 0;
  5. distance / the torpedo's OWN speed (not a closing speed) < threshold.
"""
import pytest

import App
from engine.appc import projectiles
from engine.appc.math import TGPoint3
from engine.appc.ships import ShipClass
from engine.appc.subsystems import HullSubsystem

NULL_ID = App.NULL_ID
_space = None


@pytest.fixture(autouse=True)
def _scene():
    global _space
    projectiles._active.clear()
    _space = App.SetClass_Create()
    yield
    projectiles._active.clear()
    _space = None


def _ship(x=0.0, y=0.0, z=0.0, vel=(0.0, 0.0, 0.0), where=None):
    s = ShipClass()
    s._hull = HullSubsystem("H")
    s._hull.SetMaxCondition(1000.0)
    s.SetWorldLocation(TGPoint3(x, y, z))
    if hasattr(s, "SetVelocity"):
        s.SetVelocity(TGPoint3(*vel))
    (where or _space).AddObjectToSet(s, f"Ship{id(s)}")
    return s


def _torp(pos, vel, target, source=None, where=None):
    t = projectiles.Torpedo()
    t.SetTranslateXYZ(*pos)
    t._velocity = TGPoint3(*vel)
    t._source_ship = source
    t._target_ship = target
    projectiles.register(t)
    if t.GetContainingSet() is None:
        (where or _space).AddObjectToSet(t, f"Torp{t._id}")
    return t


def _inc(ship, torp, thresh=18.0, src=NULL_ID, armed=0):
    return App.AIScriptAssist_TorpIsIncoming(ship, torp, thresh, src, armed)


def test_aimed_and_closing_within_threshold_counts():
    ship = _ship()
    assert _inc(ship, _torp((0, 100, 0), (0, -50, 0), ship)) == 1


# ── rule 3: the torpedo's target ─────────────────────────────────────────────

def test_torpedo_aimed_at_someone_else_never_counts():
    ship = _ship()
    other = _ship(0, -50, 0)
    # Flies straight through us on its way to `other`.
    assert _inc(ship, _torp((0, 100, 0), (0, -50, 0), other)) == 0


def test_unguided_torpedo_never_counts():
    ship = _ship()
    assert _inc(ship, _torp((0, 100, 0), (0, -50, 0), None)) == 0


def test_ships_own_torpedo_never_counts():
    """Launched from behind the centre toward a target ahead: closing on us by
    geometry, but aimed elsewhere."""
    ship = _ship()
    enemy = _ship(0, 300, 0)
    own = _torp((0, -1, 0), (0, 50, 0), enemy, source=ship)
    assert _inc(ship, own) == 0


# ── rule 4: moving toward the ship ───────────────────────────────────────────

def test_receding_torpedo_does_not_count():
    ship = _ship()
    assert _inc(ship, _torp((0, 100, 0), (0, 50, 0), ship)) == 0


def test_perpendicular_motion_counts_dot_zero_is_toward():
    """The dot product may be exactly 0 (at most 0)."""
    ship = _ship()
    assert _inc(ship, _torp((0, 10, 0), (50, 0, 0), ship)) == 1


def test_direction_uses_torpedo_velocity_not_relative_velocity():
    """The ship outruns a torpedo chasing it: relative velocity recedes, but
    the torpedo itself moves toward the ship, so it counts."""
    ship = _ship(vel=(0, -100, 0))
    ship_v = ship.GetVelocityTG()
    if (ship_v.x, ship_v.y, ship_v.z) != (0, -100, 0):
        pytest.skip("ShipClass velocity not settable in this fixture")
    assert _inc(ship, _torp((0, 100, 0), (0, -50, 0), ship)) == 1


# ── rule 5: distance over the torpedo's own speed ────────────────────────────

def test_time_uses_torpedo_speed_not_closing_speed():
    """Torpedo 100 GU off, moving at 50 GU/s mostly sideways: own-speed time is
    2 s; the closing-speed time would be far above the threshold."""
    ship = _ship()
    torp = _torp((0, 100, 0), (49.9, -1.0, 0), ship)   # speed ~49.91
    assert _inc(ship, torp, thresh=3.0) == 1


def test_threshold_is_strict():
    ship = _ship()
    torp = _torp((0, 100, 0), (0, -50, 0), ship)        # exactly 2.0 s
    assert _inc(ship, torp, thresh=2.0) == 0
    assert _inc(ship, torp, thresh=2.0001) == 1


def test_beyond_threshold_does_not_count():
    ship = _ship()
    assert _inc(ship, _torp((0, 1000, 0), (0, -10, 0), ship)) == 0


def test_stationary_torpedo_does_not_count():
    ship = _ship()
    assert _inc(ship, _torp((0, 100, 0), (0, 0, 0), ship)) == 0


# ── rule 2: the observing ship's set ─────────────────────────────────────────

def test_torpedo_in_another_set_does_not_count():
    ship = _ship()
    elsewhere = App.SetClass_Create()
    torp = _torp((0, 100, 0), (0, -50, 0), ship, where=elsewhere)
    assert torp.GetContainingSet() is elsewhere
    assert _inc(ship, torp) == 0


# ── rule 1: the source filter ────────────────────────────────────────────────

def test_armed_source_filter_requires_the_given_firing_object():
    ship = _ship()
    a = _ship(0, 500, 0)
    b = _ship(500, 0, 0)
    from_a = _torp((0, 100, 0), (0, -50, 0), ship, source=a)
    from_b = _torp((100, 0, 0), (-50, 0, 0), ship, source=b)
    assert _inc(ship, from_a, src=a.GetObjID(), armed=1) == 1
    assert _inc(ship, from_b, src=a.GetObjID(), armed=1) == 0
    assert _inc(ship, from_b, src=a.GetObjID(), armed=0) == 1


# ── GetIncomingTorpIDsInSet asks TorpIsIncoming about every torp in the set ──

def test_get_incoming_ids_applies_the_same_rule():
    ship = _ship()
    other = _ship(0, -50, 0)
    mine = _torp((0, 100, 0), (0, -50, 0), ship)
    theirs = _torp((0, 100, 0), (0, -50, 0), other)
    unguided = _torp((0, 90, 0), (0, -50, 0), None)
    ids = App.AIScriptAssist_GetIncomingTorpIDsInSet(
        ship, ship.GetContainingSet(), 18.0, NULL_ID, 0)
    assert ids == [mine.GetObjID()]
    assert theirs.GetObjID() not in ids and unguided.GetObjID() not in ids


def test_get_incoming_ids_only_asks_about_torps_in_the_given_set():
    ship = _ship()
    _torp((0, 100, 0), (0, -50, 0), ship)
    other_set = App.SetClass_Create()
    assert App.AIScriptAssist_GetIncomingTorpIDsInSet(
        ship, other_set, 18.0, NULL_ID, 0) == []
