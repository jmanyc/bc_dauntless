"""A stock ship's gameplay radius is the original's own ShipClass.GetRadius.

The AI's personal space (AvoidObstacles), intercept and warp-in distances read
GetRadius. Dauntless seeded it from the drawn mesh's bounding-box corner, which
is 8% short on a Galaxy and 33% long on a Kessok Heavy; no simple bound of the
mesh reproduces the original's value, so the measured value per model is used
(engine/appc/bc_ship_radii.json, stbc-oracle catalogues), and the mesh rule
stays for models without one (mods).
"""
import pytest


@pytest.mark.parametrize("cls,want", [("Galaxy", 4.36646), ("Warbird", 7.54834), ("BirdOfPrey", 1.34884),
                                      ("KessokHeavy", 6.17423)])
def test_stock_ship_radius_is_the_originals(oracle, cls, want):
    from engine import host_loop
    s = oracle(attacker="Galaxy" if cls != "Galaxy" else "Sovereign", target=cls, range_gu=100.0)
    s.tgt.SetRadius(0.0)
    host_loop._seed_ship_radius(s.tgt, 1.0, 1.0)
    assert s.tgt.GetRadius() == pytest.approx(want, abs=1e-4)


def test_an_unmeasured_model_keeps_the_mesh_rule(oracle):
    from engine import host_loop
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=100.0)
    host_loop._BC_RADII["t"] = {}          # as if the table had no entry for this model
    try:
        s.tgt.SetRadius(0.0)
        host_loop._seed_ship_radius(s.tgt, 403.0, 350.0)
        assert s.tgt.GetRadius() == pytest.approx(4.03)
    finally:
        host_loop._BC_RADII.clear()
