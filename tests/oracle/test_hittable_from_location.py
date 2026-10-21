"""ShipSubsystem.IsHittableFromLocation matches the original game.

The stock AI's FireScript adds +1 to a subsystem's targeting rating when it is
hittable from the firing ship (SDK AI/Preprocessors.py RateSubsystemForTargeting).
Dauntless returned 1.0 for everything, so phaser banks always outscored torpedo
tubes. The original's own function, called 137,484 times over a sphere of
shooter points around a Galaxy and a Sovereign (stbc-oracle
results_mac/hittable_r20_57_150.json), answers 0 or 1: a line of sight through
the hull mesh for every subsystem but energy weapons, which follow the
original's own measured direction map (twelve classes; range-independent).
The cases below are the original's answers at 57 GU (elevation, azimuth from
the nose, azimuth 90 = starboard) -- engine/appc/hull_line_of_sight.py.
"""
import math

import pytest

# (subsystem, elevation deg, azimuth deg, original's answer) -- Galaxy, 57 GU.
GALAXY_CASES = [
    ("Forward Torpedo 1", -60, 90, 1.0), ("Forward Torpedo 1", 60, 300, 0.0),
    ("Aft Torpedo 1", 90, 270, 1.0), ("Aft Torpedo 1", -90, 240, 0.0),
    ("Port Warp", 30, 30, 1.0), ("Port Warp", 0, 90, 0.0),
    ("Sensor Array", -90, 120, 1.0), ("Sensor Array", 90, 60, 0.0),
    ("Bridge", 30, 0, 1.0), ("Bridge", -30, 180, 0.0),
    ("Warp Core", -30, 180, 1.0), ("Warp Core", 30, 60, 0.0),
]


def _all_subs(ship):
    from engine.appc.combat import _iter_subsystems
    hull = ship.GetHull()
    out = list(_iter_subsystems(ship)) + [hull]
    out += [hull.GetChildSubsystem(i) for i in range(hull.GetNumChildSubsystems())]
    return {s.GetName(): s for s in out if s is not None}


def _shooter_at(ship, elev, az, r=57.0):
    """World point at (elev, az) from `ship`'s centre in its own frame."""
    import App
    ce = math.cos(math.radians(elev))
    body = (r * ce * math.sin(math.radians(az)), r * ce * math.cos(math.radians(az)), r * math.sin(math.radians(elev)))
    w = ship.GetWorldLocation()
    R = ship.GetWorldRotation()
    c = [R.GetCol(i) for i in range(3)]
    p = App.TGPoint3()
    p.SetXYZ(w.x + c[0].x * body[0] + c[1].x * body[1] + c[2].x * body[2],
             w.y + c[0].y * body[0] + c[1].y * body[1] + c[2].y * body[2],
             w.z + c[0].z * body[0] + c[1].z * body[1] + c[2].z * body[2])
    return p


def test_galaxy_energy_weapons_are_not_hittable_from_above(oracle):
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=100.0)
    subs = _all_subs(s.tgt)
    loc = _shooter_at(s.tgt, 90, 0)                # straight above: dorsal banks in plain view
    for name in ("Dorsal Phaser 1", "Ventral Phaser 2", "Forward Tractor 1", "Aft Tractor 2"):
        assert subs[name].IsHittableFromLocation(loc) == 0.0, name


def test_headless_without_meshes_keeps_everything_else_hittable(oracle, monkeypatch):
    monkeypatch.delenv("DAUNTLESS_HEADLESS_HULL_LOS", raising=False)
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=100.0)
    subs = _all_subs(s.tgt)
    for name, elev, az, _want in GALAXY_CASES:
        assert subs[name].IsHittableFromLocation(_shooter_at(s.tgt, elev, az)) == 1.0, name


@pytest.mark.parametrize("name,elev,az,want", GALAXY_CASES)
def test_line_of_sight_through_the_hull_matches_the_original(oracle, monkeypatch, name, elev, az, want):
    monkeypatch.setenv("DAUNTLESS_HEADLESS_HULL_LOS", "1")
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=100.0)
    sub = _all_subs(s.tgt)[name]
    assert sub.IsHittableFromLocation(_shooter_at(s.tgt, elev, az)) == want


@pytest.mark.parametrize("cls,name,elev,az,want", [
    # The Akira's dorsal banks: hittable from everywhere BELOW, nowhere above (the original's map).
    ("Akira", "Dorsal Phaser 1", -60, 120, 1.0), ("Akira", "Dorsal Phaser 1", 60, 120, 0.0),
    # A Keldon's port beam: hittable from almost everywhere, not from level on the starboard beam.
    ("Keldon", "Port Beam", 50, 90, 1.0), ("Keldon", "Port Beam", 10, 110, 0.0),
])
def test_energy_weapons_follow_the_originals_measured_map(oracle, cls, name, elev, az, want):
    """Energy weapons follow no geometric rule found; their measured, range-independent direction map is
    looked up (engine/appc/bc_hittable_maps.json)."""
    s = oracle(attacker="Galaxy", target=cls, range_gu=100.0)
    sub = _all_subs(s.tgt)[name]
    assert sub.IsHittableFromLocation(_shooter_at(s.tgt, elev, az)) == want
    assert sub.IsHittableFromLocation(_shooter_at(s.tgt, elev, az, r=150.0)) == want
