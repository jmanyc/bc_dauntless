"""PhaserBank.CanHit reads a bank's firing arc as the original does.

Two hardpoint shapes broke Dauntless's arc test, both common in the stock fleet:
  * an arc authored in DESCENDING order -- the Akira's four ventral banks
    (SetArcHeightAngles(-0.349066, -1.570796)), the Ambassador's ventral
    phasers, the Cardassian Hybrid's ventral beams, starbases, several tractors.
    `lo <= angle <= hi` was never true, so those banks never fired: Akira phaser
    hits fell to 7.5/min in AI duels against the original's 64.
  * a height arc running past the vertical -- Kessok and Cardassian dorsal beams
    (0.87..2.79 rad), the Marauder's ventral phaser (-2.79..0.02): the original
    also fires "over the top", behind and above.
Cases are the original's own PhaserBank.CanHit answers at 150 GU (stbc-oracle
results_mac/arcs150_*.json; azimuth from the nose, 90 = starboard). Over all 58
banks of 12 classes Dauntless now agrees on >= 99.4% of 2,664 directions each.
"""
import pytest

from tests.oracle.test_hittable_from_location import _shooter_at

# (attacker, target, which ship, bank, [(azimuth, elevation, original's answer)])
CASES = [
    ("Akira", "Nebula", "atk", "Ventral Phaser 1", [(240, -50, 1), (240, -10, 0), (90, -50, 0)]),
    ("Akira", "Nebula", "atk", "Ventral Phaser 2", [(0, -45, 1), (0, 10, 0)]),
    ("Galor", "Ambassador", "tgt", "Ventral Phaser 1", [(270, -30, 1), (270, -5, 1), (270, 10, 0), (90, -30, 0)]),
    ("KessokLight", "CardHybrid", "tgt", "Ventral Beam 1", [(185, -60, 1), (185, -10, 0), (5, -60, 0)]),
    ("KessokHeavy", "Marauder", "atk", "Dorsal Beam 1", [(180, 50, 1), (180, 10, 0), (0, 60, 1)]),
    ("KessokHeavy", "Marauder", "tgt", "Ventral Phaser", [(180, -50, 1), (0, -50, 1), (0, 30, 0)]),
]


@pytest.mark.parametrize("atk,tgt,which,bank,cells", CASES, ids=[c[0] + "-" + c[3] for c in CASES])
def test_bank_fires_where_the_original_does(oracle, atk, tgt, which, bank, cells):
    s = oracle(attacker=atk, target=tgt, range_gu=3000.0)
    ship = getattr(s, which)
    ws = ship.GetPhaserSystem()
    banks = {ws.GetChildSubsystem(i).GetName(): ws.GetChildSubsystem(i) for i in range(ws.GetNumChildSubsystems())}
    for az, el, want in cells:
        assert int(bool(banks[bank].CanHit(_shooter_at(ship, el, az, 150.0)))) == want, (az, el)
