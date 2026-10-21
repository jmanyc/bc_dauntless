"""A dormant branch a priority list scans is active down its whole path.

A cloaking ship's Quick Battle AI (CloakAttackWrapper) holds an ordinary attack
AI inside the "CloakingDisabled" branch, dormant while the cloak works. On the
original that branch and the preprocessors inside it (FireAll, FireAll2,
FirePulseOnly) are active the whole fight and its conditions keep evaluating
(stbc-oracle duels_ai Warbird/BirdOfPrey, OracleAIProbe xv/xk rows). In
Dauntless its BuilderAI was never even built, so the incoming-torpedo watcher
it shares by name with the live branch lost its periodic check, latched a
missed plasma torpedo, and a Bird of Prey evaded for 95% of a fight (original
34-64%) with its forward cannons turned away: 10 bolts in three minutes.
"""
import importlib


def _quick_battle(oracle, monkeypatch):
    s = oracle(attacker="BirdOfPrey", target="Warbird", range_gu=100.0)
    s.attach_basic_attack(1.0)
    QB = importlib.import_module("QuickBattle.QuickBattle")
    monkeypatch.setattr(QB, "pFriendlies", s.mission.GetEnemyGroup(), raising=False)
    monkeypatch.setattr(QB, "pEnemies", s.mission.GetFriendlyGroup(), raising=False)
    monkeypatch.setattr(QB, "g_iCurrentAILevel", 1.0, raising=False)
    s.atk.SetAI(importlib.import_module("QuickBattle.QuickBattleAI").CreateAI(s.atk), 0, 0)
    s.tgt.SetAI(importlib.import_module("QuickBattle.QuickBattleFriendlyAI").CreateAI(s.tgt), 0, 0)
    return s


def _find(node, name, depth=0):
    if node is None or depth > 40:
        return None
    if node.GetName() == name:
        return node
    kids = [c[1] if isinstance(c, tuple) else c for c in (node.__dict__.get("_ais") or [])]
    for child in [node.__dict__.get("_contained_ai")] + kids:
        if child is not None:
            hit = _find(child, name, depth + 1)
            if hit is not None:
                return hit
    return None


# First in the file: the incoming-torpedo watchers are shared by ship NAME, so a
# fight left over from an earlier test can keep this one's watcher ticking.
def test_a_bird_of_prey_does_not_latch_evasion(oracle, monkeypatch):
    s = _quick_battle(oracle, monkeypatch)
    cls = importlib.import_module("AI.PlainAI.EvadeTorps").EvadeTorps
    ran = []
    orig = cls.Update

    def upd(self, *a, **k):
        if self.pCodeAI.GetShip() is s.atk:
            ran.append(s.t)
        return orig(self, *a, **k)
    monkeypatch.setattr(cls, "Update", upd)
    seconds = set()
    for _ in s.sample_until(90.0, every_ticks=60):
        pass
    seconds = {int(t) for t in ran if t >= 8.0}
    assert len(seconds) < 0.7 * 82, "evading %d of 82 s" % len(seconds)


def test_the_dormant_branch_is_built_and_active(oracle, monkeypatch):
    s = _quick_battle(oracle, monkeypatch)
    for _ in s.sample_until(12.0, every_ticks=60):
        pass
    branch = _find(s.atk.GetAI(), "CloakingDisabled")
    # _is_active_in_tree is the original's IsActive (Dauntless's IsActive()
    # reports the node's own status on containers).
    assert branch is not None and branch._is_active_in_tree
    fire_all = _find(branch, "FireAll")
    assert fire_all is not None, "the ordinary attack AI inside the dormant branch was never built"
    assert fire_all._is_active_in_tree
    # Free both trees: their conditions share the incoming-torpedo watchers by
    # ship NAME, and live ones would keep the next test's watcher ticking.
    s.atk.ClearAI()
    s.tgt.ClearAI()
