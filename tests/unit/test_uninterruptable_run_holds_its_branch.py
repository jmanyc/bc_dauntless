"""A RUNNING SetInterruptable(0) node holds its ancestors; only its own
conditions end it -- and a stale one that is not running holds nothing.

FedAttack's TorpRun_3 sits in TorpsReadyNotInterruptable (SetInterruptable(0))
under TorpsReadyAndNotInEnemyFiringArc, whose EvalFunc needs "not in the
enemy's phaser arc". On the original (stbc-oracle duels_ai Galaxy/Sovereign,
xy focus vs xk conditions) the run carried on through 636 of 1,224 samples in
which that arc condition was true. Dauntless dropped the run the moment the
parent went dormant (TorpRun_3 4-5% of a fight vs the original's 20%).

The hold needs the run to have focus: FollowThroughWarp's WarpFollow is also
SetInterruptable(0) and its Update stays ACTIVE while the target is in the same
set, so a WarpFollow touched once at tree build (before ConditionAnyInSameSet
initialised) must not come back and hold the ship out of the fight.
"""
from engine.appc.ai import (ArtificialIntelligence, ConditionalAI_Create, PlainAI_Create,
                            PriorityListAI_Create, TGCondition)
from engine.appc.ai_driver import tick_ai

US_ACTIVE = ArtificialIntelligence.US_ACTIVE
US_DORMANT = ArtificialIntelligence.US_DORMANT


class _Script:
    def __init__(self):
        self.status = US_ACTIVE

    def GetNextUpdateTime(self):
        return 0.0

    def Update(self):
        return self.status


def _leaf(name):
    ai = PlainAI_Create(None, name)
    ai._script_instance = _Script()
    return ai


def _tree():
    flags = {"parent": True, "own": True}
    own = ConditionalAI_Create(None, "OwnNotInterruptable")
    own.SetInterruptable(0)
    own.AddCondition(TGCondition())
    own.SetEvaluationFunction(lambda *_a: US_ACTIVE if flags["own"] else US_DORMANT)
    own.SetContainedAI(_leaf("run"))
    parent = ConditionalAI_Create(None, "Parent")
    parent.AddCondition(TGCondition())
    parent.SetEvaluationFunction(lambda *_a: US_ACTIVE if flags["parent"] else US_DORMANT)
    parent.SetContainedAI(own)
    other = ConditionalAI_Create(None, "Other")
    other.AddCondition(TGCondition())
    other.SetEvaluationFunction(lambda *_a: US_ACTIVE)
    other.SetContainedAI(_leaf("other"))
    root = PriorityListAI_Create(None, "root")
    root.AddAI(parent, 1)
    root.AddAI(other, 2)
    return flags, root, parent, own, other


def test_the_parents_conditions_do_not_end_the_run():
    flags, root, parent, own, other = _tree()
    tick_ai(root, 0.0)
    assert parent._status == US_ACTIVE and own._status == US_ACTIVE

    flags["parent"] = False                 # e.g. the turn brought the ship into the enemy's arc
    tick_ai(root, 1.0)
    assert parent._status == US_ACTIVE, "a running uninterruptable node holds the parent"
    assert root.__dict__.get("_running_child") is parent


def test_its_own_conditions_end_the_run():
    flags, root, parent, own, other = _tree()
    tick_ai(root, 0.0)
    flags["parent"] = False
    flags["own"] = False                    # e.g. the torpedoes are gone
    tick_ai(root, 1.0)
    tick_ai(root, 2.0)
    assert own._status != US_ACTIVE
    assert root.__dict__.get("_running_child") is other


def test_a_stale_uninterruptable_leaf_that_is_not_running_holds_nothing():
    flags, root, parent, own, other = _tree()
    leaf = own._contained_ai
    tick_ai(root, 0.0)
    flags["parent"] = False
    flags["own"] = False
    tick_ai(root, 1.0)                      # the branch is left; the leaf keeps US_ACTIVE
    assert leaf._status == US_ACTIVE and not leaf._has_focus
    flags["own"] = True                     # the node would be ACTIVE again, but nothing below runs
    leaf.SetInterruptable(0)
    tick_ai(root, 2.0)
    assert root.__dict__.get("_running_child") is other, "a stale leaf must not pull the list back"
