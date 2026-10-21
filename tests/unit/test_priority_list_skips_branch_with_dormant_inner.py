"""A priority list passes over an ACTIVE ConditionalAI whose contained ConditionalAI is dormant.

Measured on the original (stbc-oracle results_win/duels_ai_matrix, Galaxy FedAttack, OracleAIProbe focus
and active bits, 31,169 samples inside the TooClose branch): FrontTorpsReadyFrontShieldsOk -- active
whenever torpedoes are ready -- holds focus ONLY while its contained SignificantTimePassed (a 10 s
ConditionTimer) is active; otherwise the list runs the siblings below it (ICO_WeaponBias, ICO_MoveWithWeapons,
Flee) while the outer branch stays active-but-unfocused. Dauntless gave the list to the outer branch and
stopped, so no behaviour ran for 14.8% of a Galaxy's fight time (matrix r2) and the ship coasted on its
last command at close range (engine/appc/ai_driver.py:_contained_conditional_dormant).
"""
from engine.appc.ai import ArtificialIntelligence, ConditionalAI, PlainAI, PriorityListAI, TGCondition
from engine.appc.ai_driver import tick_ai
from engine.appc.ships import ShipClass

ACTIVE, DORMANT = ArtificialIntelligence.US_ACTIVE, ArtificialIntelligence.US_DORMANT


class _Leaf:
    def __init__(self):
        self.calls = 0

    def Update(self):
        self.calls += 1
        return ACTIVE

    def GetNextUpdateTime(self):
        return 0.0


def _gate(b):
    return ACTIVE if b else DORMANT


def _cond(name, cond, contained):
    c = ConditionalAI(ShipClass(), name)
    c.SetContainedAI(contained)
    c.AddCondition(cond)
    c.SetEvaluationFunction(_gate)
    return c


def _plain(leaf):
    p = PlainAI(ShipClass(), "leaf")
    p._script_instance = leaf
    return p


def _tree():
    torps_ready, time_passed, fallback = TGCondition(), TGCondition(), TGCondition()
    torps_ready.SetStatus(1)
    fallback.SetStatus(1)
    run_leaf, side_leaf = _Leaf(), _Leaf()
    inner = _cond("SignificantTimePassed", time_passed, _plain(run_leaf))
    outer = _cond("FrontTorpsReadyFrontShieldsOk", torps_ready, inner)
    side = _cond("UseSideArcs_ShortTime", fallback, _plain(side_leaf))
    pl = PriorityListAI(ShipClass(), "TooClosePriorities")
    pl.AddAI(outer, 1)
    pl.AddAI(side, 2)
    return pl, outer, time_passed, run_leaf, side_leaf


def test_the_list_runs_the_next_sibling_while_the_inner_branch_is_dormant():
    pl, outer, time_passed, run_leaf, side_leaf = _tree()
    for t in range(5):
        tick_ai(pl, float(t))
    assert run_leaf.calls == 0
    assert side_leaf.calls >= 4          # one tick may go to the outer branch before it reads dormant


def test_the_outer_branch_takes_the_list_back_when_the_inner_wakes():
    pl, outer, time_passed, run_leaf, side_leaf = _tree()
    for t in range(3):
        tick_ai(pl, float(t))
    time_passed.SetStatus(1)
    before = side_leaf.calls
    for t in range(3, 6):
        tick_ai(pl, float(t))
    assert run_leaf.calls >= 2
    assert side_leaf.calls == before
