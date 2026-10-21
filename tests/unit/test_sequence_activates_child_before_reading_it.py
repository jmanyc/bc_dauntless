"""A SequenceAI activates its child as it reaches it, before reading the child's conditions.

Activation re-arms a ConditionTimer (bResetOnActivate). CloakAttack's OuterSequence loops
[FarEnough_TimeNotPassed (15 s timer), TooClose_ShortTime (60 s timer)]; reading a newly reached child's
stale, expired timer first made both children answer DONE and the loop spun at point blank. Measured on the
original (stbc-oracle results_win/duels_ai_matrix, Vor'cha vs Galaxy, OracleAIProbe xk rows): the state
"within 50 GU, 15 s passed, within 100 GU, 60 s passed" held 1.5% of the fight; Dauntless 25.5% before the
fix, 0.1% after (engine/appc/ai_driver.py:_tick_sequence).
"""
from engine.appc.ai import ArtificialIntelligence, ConditionalAI, PlainAI, SequenceAI, TGCondition
from engine.appc.ai_driver import tick_ai
from engine.appc.ships import ShipClass

ACTIVE, DONE = ArtificialIntelligence.US_ACTIVE, ArtificialIntelligence.US_DONE


class _ResettingTimer(TGCondition):
    """A condition that reads 1 (expired) until Activate() re-arms it, like ConditionTimer."""
    def __init__(self):
        super().__init__()
        self.SetStatus(1)
        self.activations = 0

    def SetActive(self, *args):              # ConditionScript forwards node activation to Activate()
        super().SetActive(*args)
        self.Activate()

    def Activate(self):
        self.activations += 1
        self.SetStatus(0)


class _Leaf:
    def __init__(self):
        self.calls = 0

    def Update(self):
        self.calls += 1
        return ACTIVE

    def GetNextUpdateTime(self):
        return 0.0


def _done_when_expired(expired):
    return DONE if expired else ACTIVE


def test_a_reached_child_is_rearmed_before_its_status_is_read():
    first_done = TGCondition()
    first_done.SetStatus(1)
    first = ConditionalAI(ShipClass(), "First")
    first.SetContainedAI(PlainAI(ShipClass(), "a"))
    first.AddCondition(first_done)
    first.SetEvaluationFunction(lambda b: DONE if b else ACTIVE)

    timer = _ResettingTimer()
    leaf = _Leaf()
    plain = PlainAI(ShipClass(), "leaf")
    plain._script_instance = leaf
    second = ConditionalAI(ShipClass(), "TooClose_ShortTime")
    second.SetContainedAI(plain)
    second.AddCondition(timer)
    second.SetEvaluationFunction(_done_when_expired)

    seq = SequenceAI(ShipClass(), "OuterSequence")
    seq.AddAI(first)
    seq.AddAI(second)
    seq.SetLoopCount(-1)
    # The timer ran out on an earlier pass (AddCondition armed it at wiring); the branch is not active now.
    timer.SetStatus(1)
    timer.activations = 0
    for t in range(3):
        tick_ai(seq, float(t))
    assert timer.activations >= 1
    assert leaf.calls >= 1, "the reached child read its stale expired timer and was skipped as DONE"
