"""Weapon.CalculateRoughDirection matches the original's, in the ship's frame.

PhaserSweep turns a bank's rough direction onto its target and
IntelligentCircleObject rolls to present weapons by it. Dauntless returned the
mount forward for phaser banks (so a sweeping ship pointed its nose at the
enemy: target in the 30 deg nose cone 40-66% of a sweep vs the original's
18-20%) and a zero vector for pulse cannons and tractors. Measured on stbc.exe
(stbc-oracle results_mac/rough_*.json, 152 weapons of 14 classes, all now
within 1 deg): a phaser bank points at the middle of its arcs; a pulse cannon or
tractor at min/2 + middle on each axis; a torpedo tube along its tube.
"""
import math

import pytest

from tests.oracle.test_hittable_from_location import _all_subs

# (attacker, target, which, weapon, the original's direction in the model frame)
CASES = [
    ("Galaxy", "Sovereign", "atk", "Dorsal Phaser 2", (-0.4394, 0.7611, 0.4772)),
    ("Galaxy", "Sovereign", "tgt", "Ventral Phaser 1", (-0.8141, 0.0, -0.5807)),
    ("Galaxy", "Sovereign", "atk", "Forward Tractor 2", (-0.1913, 0.3314, -0.9239)),
    ("Galaxy", "Sovereign", "atk", "Aft Torpedo 1", (0.0, -1.0, 0.0)),
    ("Warbird", "BirdOfPrey", "tgt", "Star Cannon", (-0.2113, 0.9532, -0.2164)),
    ("KessokLight", "CardHybrid", "tgt", "Aft Tractor", (0.1485, -0.554, 0.8192)),
]


@pytest.mark.parametrize("atk,tgt,which,name,want", CASES, ids=[c[0] + "-" + c[3] for c in CASES])
def test_rough_direction(oracle, atk, tgt, which, name, want):
    s = oracle(attacker=atk, target=tgt, range_gu=3000.0)
    ship = getattr(s, which)
    w = _all_subs(ship)[name].CalculateRoughDirection()
    R = ship.GetWorldRotation()
    got = [w.x * R.GetCol(i).x + w.y * R.GetCol(i).y + w.z * R.GetCol(i).z for i in range(3)]
    cos = sum(g * x for g, x in zip(got, want)) / (math.sqrt(sum(g * g for g in got)) or 1.0)
    assert math.degrees(math.acos(max(-1.0, min(1.0, cos)))) < 1.0, got
