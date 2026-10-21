"""A dormant higher-priority branch keeps its conditions evaluating, so it can wake.

Several stock conditions refresh only while active: ConditionInPhaserFiringArc,
ConditionFacingToward and ConditionIncomingTorps poll `if
self.pCodeCondition.IsActive()`, and ConditionTimer re-arms in Activate(). In
FedAttack the torpedo-run branch "TorpsReadyAndNotInEnemyFiringArc" (priority 1
of TooFarPriorities) starts DORMANT -- the ships begin inside each other's
phaser arcs. Dauntless refreshed the dormant branch's status but never
activated it, so its arc condition stayed at that creation value for the whole
fight: half the original's torpedo launches and no torpedo evasion. The
original keeps every child at or above the running one active (stbc-oracle
OracleAIProbe: that branch is active exactly as long as its list).
"""
import App  # noqa: F401 - the shim must be live before the AI classes are used
from engine.appc.ai import (ArtificialIntelligence, ConditionalAI_Create, ConditionScript_Create,
                            PlainAI_Create, PriorityListAI_Create)
from engine.appc.ai_driver import tick_ai

US_ACTIVE = ArtificialIntelligence.US_ACTIVE
US_DORMANT = ArtificialIntelligence.US_DORMANT


class PolledCondition:
    """A condition script shaped like the stock polled ones: it can only change
    its status after Activate() arms it."""
    activations = 0

    def __init__(self, pCodeCondition):
        self.pCodeCondition = pCodeCondition
        self.pCodeCondition.SetStatus(0)

    def Activate(self):
        PolledCondition.activations += 1
        if self.pCodeCondition.IsActive():
            self.pCodeCondition.SetStatus(1)


def test_a_dormant_branch_wakes_when_its_polled_condition_turns_true():
    PolledCondition.activations = 0
    cond = ConditionScript_Create(__name__, "PolledCondition")

    def needs_condition(bTrue):
        return US_ACTIVE if bTrue else US_DORMANT

    run = ConditionalAI_Create(None, "TorpsReadyAndNotInEnemyFiringArc")
    run.SetEvaluationFunction(needs_condition)
    run.AddCondition(cond)
    run.SetContainedAI(PlainAI_Create(None, "TorpRun"))

    fallback = ConditionalAI_Create(None, "FwdTorpsReady")
    fallback.SetEvaluationFunction(lambda *s: US_ACTIVE)
    fallback.SetContainedAI(PlainAI_Create(None, "TorpRun_2"))

    root = PriorityListAI_Create(None, "TooFarPriorities")
    root.AddAI(run, 1)
    root.AddAI(fallback, 3)

    tick_ai(root, 0.0)                 # branch dormant: the list runs the fallback...
    assert run._is_active_in_tree is True, "...but the scanned dormant branch is active in the tree"
    assert PolledCondition.activations >= 1, "its condition was armed"
    assert cond.GetStatus() == 1
    tick_ai(root, 1.0)                 # the armed condition is true: the branch takes the list
    assert run._status == US_ACTIVE
