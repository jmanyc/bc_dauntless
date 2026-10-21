"""A polled condition keeps evaluating while its branch is not running.

FedAttack's ConditionFacingToward (own nose within 45 deg of the target) lives in
TorpsStillReadyFacingTowardNotInterruptable, a branch the original never reports
active; yet on stbc.exe its status matches the ships' real geometry on
98.9-99.2% of samples from the first second (stbc-oracle duels_ai Galaxy vs
Sovereign, OracleAIProbe xk rows against xm/xo poses). Its PeriodicCheck runs
only `if self.pCodeCondition.IsActive()`, so a condition must stay active when
its node is not. Dauntless deactivated it with its node: the status latched
true for 97% of a fight (real geometry: 28%) and gated FedAttack's
"torps ready and not in the enemy's arc" torpedo run.
"""
import importlib
import math


def test_facing_toward_tracks_the_geometry(oracle, monkeypatch):
    import App
    made = []
    create = App.ConditionScript_Create

    def spy(*a, **k):
        o = create(*a, **k)
        if a[1] == "ConditionFacingToward":
            made.append((o, a[2]))
        return o
    monkeypatch.setattr(App, "ConditionScript_Create", spy)

    s = oracle(attacker="Galaxy", target="Sovereign", range_gu=100.0)
    s.attach_basic_attack(1.0)
    QB = importlib.import_module("QuickBattle.QuickBattle")
    monkeypatch.setattr(QB, "pFriendlies", s.mission.GetEnemyGroup(), raising=False)
    monkeypatch.setattr(QB, "pEnemies", s.mission.GetFriendlyGroup(), raising=False)
    monkeypatch.setattr(QB, "g_iCurrentAILevel", 1.0, raising=False)
    s.atk.SetAI(importlib.import_module("QuickBattle.QuickBattleAI").CreateAI(s.atk), 0, 0)
    s.tgt.SetAI(importlib.import_module("QuickBattle.QuickBattleFriendlyAI").CreateAI(s.tgt), 0, 0)
    for _ in s.sample_until(10.0, every_ticks=60):      # the Wait and the tree build
        pass
    cond = [o for o, owner in made if owner == s.atk.GetName()]
    assert len(cond) == 1

    agree = n = 0
    for _ in s.sample_until(120.0, every_ticks=30):
        if s.atk.IsDead() or s.tgt.IsDead():
            break
        f = s.atk.GetWorldRotation().GetCol(1)
        a, b = s.atk.GetWorldLocation(), s.tgt.GetWorldLocation()
        d = [b.x - a.x, b.y - a.y, b.z - a.z]
        facing = (f.x * d[0] + f.y * d[1] + f.z * d[2]) / math.sqrt(sum(x * x for x in d)) >= math.cos(math.radians(45))
        agree += int(bool(cond[0].GetStatus()) == facing)
        n += 1
    assert n > 100
    assert agree / n > 0.9, "FacingToward agreed with the geometry on %.0f%% of samples" % (100.0 * agree / n)
