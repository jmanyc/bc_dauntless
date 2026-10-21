"""Energy-weapon damage points match the original game.

A hit damages the subsystems whose footprint it overlaps, measured from each
subsystem's DAMAGE point. For an energy weapon whose position lies outside the
hull (a phaser bank's strip midpoint on the saucer rim) the original walks
toward the ship's centre to the first hull crossing and 0.05 GU beyond
(engine/appc/hull_line_of_sight.py). Dauntless used the position, so saucer-rim
hits kept wearing down the Galaxy's ventral banks, and its AI kept targeting
them (Dauntless 20-24% of aims vs the original's 1-5%). The values are
stbc.exe's own ShipSubsystem.GetDamagePoint (stbc-oracle catalogue,
results_mac/hittable_r20_57_150.json); replaying the game's recorded hits with
them lifts the Sovereign's same-subsystems score from 80% to 87% (the rest are
hits on an already destroyed tractor).
"""
import math

import pytest

BC = {
    "Galaxy": {
        "Ventral Phaser 1": (-0.0098, 0.0096, 0.0012), "Ventral Phaser 2": (-0.5985, 1.9573, 0.144),
        "Ventral Phaser 3": (0.5985, 1.9573, 0.144), "Ventral Phaser 4": (0.0098, 0.0096, 0.0012),
        "Dorsal Phaser 1": (-1.5526, 1.1667, 0.4593), "Dorsal Phaser 2": (-0.7996, 2.3082, 0.4732),
        "Dorsal Phaser 3": (0.7996, 2.3082, 0.4732), "Dorsal Phaser 4": (1.5526, 1.1667, 0.4593),
        "Aft Tractor 1": (0.0, 0.1609, 0.5094), "Forward Tractor 1": (0.0, 2.32, 0.5027),
        "Forward Tractor 2": (0.0, 0.0069, 0.0002),
    },
    "Sovereign": {
        "Ventral Phaser 1": (-0.7017, 1.1471, -0.0506), "Ventral Phaser 2": (-0.5423, 2.2927, -0.0608),
        "Ventral Phaser 3": (0.5423, 2.2927, -0.0608), "Ventral Phaser 4": (0.7017, 1.1471, -0.0506),
        "Dorsal Phaser 1": (-0.5997, 1.3707, 0.1199), "Dorsal Phaser 2": (-0.4171, 2.2218, 0.1298),
        "Dorsal Phaser 3": (0.4171, 2.2218, 0.1298), "Dorsal Phaser 4": (0.5997, 1.3707, 0.1199),
        "Aft Tractor 1": (0.0, -0.016, -0.0055), "Aft Tractor 2": (0.0, -1.6764, -0.1425),
        "Forward Tractor 1": (0.0, 3.0062, 0.0679), "Forward Tractor 2": (0.0, 2.741, -0.0731),
    },
}


def _subs(ship):
    from engine.appc.combat import _iter_subsystems
    return {s.GetName(): s for s in _iter_subsystems(ship)}


@pytest.mark.parametrize("cls", ["Galaxy", "Sovereign"])
def test_energy_weapon_damage_points_match_the_original(oracle, cls):
    s = oracle(attacker="Sovereign" if cls == "Galaxy" else "Galaxy", target=cls, range_gu=3000.0)
    subs = _subs(s.tgt)
    for name, want in BC[cls].items():
        p = subs[name].GetDamagePoint()
        assert math.dist((p.x, p.y, p.z), want) < 0.01, (name, (p.x, p.y, p.z), want)


def test_the_galaxys_aft_tractor_2_is_the_one_known_miss(oracle):
    """0.05 GU outside the drawn mesh, inside the original's coarser hull mask:
    the original keeps the position (0, -1, -0.73); this moves it 0.1 GU in."""
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=3000.0)
    p = _subs(s.tgt)["Aft Tractor 2"].GetDamagePoint()
    assert math.dist((p.x, p.y, p.z), (0.0, -1.0, -0.73)) < 0.12


def test_a_saucer_rim_hit_no_longer_reaches_the_galaxys_ventral_bank(oracle):
    """A phaser hit on the saucer underside at Ventral Phaser 1's strip midpoint:
    the original damages nothing there (its damage point is at the centre)."""
    import App
    from engine.appc import combat
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=3000.0)
    bank = _subs(s.tgt)["Ventral Phaser 1"]
    c0 = bank.GetCondition()
    p = App.TGPoint3(); p.SetXYZ(-1.33, 1.3, 0.16)
    combat.apply_hit(s.tgt, 200.0, p, s.atk, splash_radius=0.15, bypass_shields=True)
    assert bank.GetCondition() == c0
