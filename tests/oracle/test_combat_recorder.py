"""The live combat recorder (engine/combat_recorder.py) writes a human-played fight in the duel harness's format.

Human runs are compared with the original's oracle duel telemetry by the same scripts that read the headless
matrix (stbc-oracle/scorecard/dauntless_aim_duels.py output), so the file must carry those fields; the human
additions (player command events, target, alert, speed) must be there too. A player-controlled Akira against
an AI Sovereign, as in the first human session.
"""
import json

import pytest

from engine import combat_recorder as cr


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(cr, "ENABLED", True)
    cr.install()
    yield tmp_path
    cr.uninstall()


def _run(s, seconds, player):
    for _t, _sc in s.sample_until(seconds):
        cr.tick(player)


def test_a_fight_is_recorded_in_the_duel_harness_format(oracle, recorder):
    s = oracle(attacker="Sovereign", target="Akira", range_gu=100.0)
    s.attach_basic_attack(1.0)                 # built after install(): its AI tree is recorded
    _run(s, 1.0, s.tgt)                        # the fight opens on the first frame with both ships
    s.tgt.SetTarget(s.atk)                     # the "player" acts: a target pick and an impulse order
    s.full_impulse(ship=s.tgt, fraction=0.5)
    _run(s, 29.0, s.tgt)
    cr.tick(None)                              # the player went away: the fight is closed and written

    files = list(recorder.glob("duel_Sovereign_enemy*_vs_Akira_player_human_*.json"))
    assert len(files) == 1
    d = json.loads(files[0].read_text())
    for key in ("track", "allhits", "hitdetail", "choices", "fs_counts", "tree_nodes", "cond_nodes",
                "tube_names", "events", "attacker_name", "target_name", "t_end"):
        assert key in d, key
    assert d["human"] == 1 and d["attacker_name"] == s.atk.GetName() and d["target_name"] == s.tgt.GetName()

    tr = d["track"]
    assert 280 <= len(tr) <= 310, "one row per 0.1 game seconds"
    row = tr[-1]
    for key in ("t", "A", "T", "sA", "sT", "tp", "spd", "beh", "tree", "cond"):
        assert key in row, key
    assert len(row["A"]) == 9 and len(row["T"]) == 9
    for key in ("imp", "ta", "td", "ew", "sh", "hu", "tg", "al", "vel"):
        assert key in row["sT"], key
    assert row["sT"]["tg"] == s.atk.GetName()

    assert d["tree_nodes"] and set(sh for sh, _k, _n in d["tree_nodes"]) == {s.atk.GetName()}
    assert len(row["tree"]) == 2 * len(d["tree_nodes"]), "focus+active flag pair per node, as the harness"
    assert any(k.startswith(s.atk.GetName() + "|") for r in tr for k in r["beh"]), "AI behaviour calls"
    assert any(h[2] == s.tgt.GetName() for h in d["allhits"]), "the Sovereign's hits on the Akira"

    ev = {(e[1], e[3]) for e in d["events"]}
    assert ("SetTarget", "-") in ev and ("SetImpulse", "-") in ev, "player orders, issued outside any AI script"


def test_the_fight_ends_when_a_ship_dies(oracle, recorder):
    s = oracle(attacker="Sovereign", target="Akira", range_gu=100.0)
    _run(s, 1.0, s.tgt)
    from engine.appc import combat             # the enemy is destroyed by a hit, as in play
    combat.apply_hit(s.atk, 1.0e7, s.atk.GetWorldLocation(), s.tgt, weapon_type="phaser", bypass_shields=True)
    assert s.atk.IsDying() or s.atk.IsDead()
    _run(s, cr.TAIL_S + 1.0, s.tgt)
    files = list(recorder.glob("*.json"))
    assert len(files) == 1
    d = json.loads(files[0].read_text())
    assert d["finished"] and d["winner_role"] == "player" and d["winner"] == "Akira"


def test_uninstall_restores_every_wrapped_attribute(oracle, recorder):
    from engine.appc import combat
    hooked = combat.apply_hit
    cr.uninstall()
    assert combat.apply_hit is not hooked
    cr.install()                               # the fixture's teardown uninstalls again


def test_player_commands_and_torpedo_launches_are_events(oracle, recorder):
    """What the player DID, not just the resulting state: fire presses (with target and target subsystem), power
    and ammo orders, and each torpedo launch by either ship with what it was aimed at -- the twin of the
    original's xu rows (stbc-oracle Oracle/OracleHuman.py)."""
    s = oracle(attacker="Sovereign", target="Akira", range_gu=60.0)
    s.attach_basic_attack(1.0)
    for g in ("GetPhaserSystem", "GetTorpedoSystem"):
        ws = getattr(s.tgt, g)()
        ws.SetCondition(ws.GetMaxCondition())
    _run(s, 1.0, s.tgt)
    s.tgt.SetTarget(s.atk)
    imp = s.atk.GetImpulseEngineSubsystem()
    s.tgt.SetTargetSubsystem(imp)
    s.tgt.GetShields().SetPowerPercentageWanted(1.25)
    s.tgt.GetShields().SetPowerPercentageWanted(1.25)          # the same value again is not a new order
    s.tgt.GetTorpedoSystem().StartFiring(s.atk)
    _run(s, 4.0, s.tgt)
    s.tgt.GetTorpedoSystem().StopFiring()
    _run(s, 20.0, s.tgt)
    cr.tick(None)
    d = json.loads(next(iter(recorder.glob("*.json"))).read_text())
    ev = d["events"]
    starts = [e for e in ev if e[1] == "StartFiring"]
    assert starts and starts[0][4] == s.atk.GetName() and starts[0][5] == imp.GetName()
    assert any(e[1] == "StopFiring" for e in ev)
    assert len([e for e in ev if e[1] == "SetPowerPercentageWanted"]) == 1
    mine = [e for e in ev if e[1] == "TorpedoLaunch" and e[3] == s.tgt.GetName()]
    assert mine and mine[0][5] == imp.GetName(), "the player's launch, aimed at the impulse engines"


def test_periodic_flushes_append_a_journal_and_it_rebuilds_the_fight(oracle, recorder):
    """Mid-fight flushes append only the new rows to <name>.part.jsonl -- rewriting the whole growing file stalled
    the frame for up to 0.3 s every 10 s in the first Dauntless human run (dn_human_1) -- and a killed game's
    journal rebuilds the same rows the finished file holds. The journal goes once the full file is written."""
    s = oracle(attacker="Sovereign", target="Akira", range_gu=100.0)
    s.attach_basic_attack(1.0)
    _run(s, 1.0, s.tgt)
    s.tgt.SetTarget(s.atk)
    _run(s, 2.5 * cr.FLUSH_S, s.tgt)
    parts = list(recorder.glob("*.part.jsonl"))
    assert len(parts) == 1 and not list(recorder.glob("*.json")), "flushes journal; the full file waits for the end"
    assert len(parts[0].read_text().splitlines()) == 2
    mid = cr.read_partial(str(parts[0]))
    cr.tick(None)
    assert not list(recorder.glob("*.part.jsonl"))
    d = json.loads(next(iter(recorder.glob("*.json"))).read_text())
    assert mid["finished"] is False and mid["attacker_name"] == d["attacker_name"]
    for k in ("track", "allhits", "hitdetail", "choices", "hits", "events"):
        assert mid[k] == d[k][:len(mid[k])], k
    assert len(mid["track"]) >= 0.8 * 2 * cr.FLUSH_S / cr.SAMPLE_DT
    assert mid["fs_counts"]["_smart"] == d["fs_counts"]["_smart"][:len(mid["fs_counts"]["_smart"])]


def test_power_rows_cover_only_real_power_consumers(oracle, recorder):
    """`pq` lists powered subsystems only: the hull and the reactor are not consumers (they used to appear with stub
    values, because engine objects answer hasattr() for any attribute)."""
    s = oracle(attacker="Sovereign", target="Akira", range_gu=100.0)
    _run(s, 1.0, s.tgt); cr.tick(None)
    d = json.loads(next(iter(recorder.glob("*.json"))).read_text())
    pq = d["track"][-1]["sT"]["pq"]
    assert pq and "Hull" not in pq and "Warp_Core" not in pq
    assert all(isinstance(v[0], float) or isinstance(v[0], int) for v in pq.values())
