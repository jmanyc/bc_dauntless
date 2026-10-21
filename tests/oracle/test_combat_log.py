"""The opt-in combat log (DAUNTLESS_COMBAT_LOG=1) writes one JSON line per hit."""
import json


def test_combat_log_records_a_hit(oracle, monkeypatch, tmp_path):
    from engine.appc import combat_log
    monkeypatch.setattr(combat_log, "_enabled", True)
    out = tmp_path / "combat.jsonl"
    monkeypatch.setattr(combat_log, "_path", str(out))
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=3000.0)
    import App
    from engine.appc import combat
    p = App.TGPoint3()
    p.SetXYZ(0.0, -0.36, -0.47)
    combat.apply_hit(s.tgt, 550.0, p, s.atk, splash_radius=0.13, weapon_type="torpedo")
    rec = json.loads(out.read_text().splitlines()[-1])
    assert rec["target"] == s.tgt.GetName() and rec["weapon"] == "torpedo"
    assert rec["dmg"] == 550.0 and rec["r"] == 0.13
    assert rec["absorbed"] + rec["leaked"] == 550.0
    assert len(rec["faces_after"]) == 6


def test_combat_log_off_by_default(oracle, monkeypatch, tmp_path):
    from engine.appc import combat_log
    monkeypatch.setattr(combat_log, "_enabled", False)
    out = tmp_path / "combat.jsonl"
    monkeypatch.setattr(combat_log, "_path", str(out))
    s = oracle(attacker="Sovereign", target="Galaxy", range_gu=3000.0)
    import App
    from engine.appc import combat
    p = App.TGPoint3()
    p.SetXYZ(0.0, -0.36, -0.47)
    combat.apply_hit(s.tgt, 550.0, p, s.atk, splash_radius=0.13)
    assert not out.exists()
