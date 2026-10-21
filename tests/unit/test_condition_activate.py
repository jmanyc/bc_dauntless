"""ConditionScript.SetActive forwards to the wrapped script's Activate().

Real Appc's ConditionScript::SetActive calls the Python instance's optional
Activate(); ConditionalAI drives SetActive/SetInactive across its condition
list. ConditionTimer (65 SDK uses) re-arms its timer in Activate() — without
the forward it fires once and latches true forever.
"""
from engine.appc.ai import ConditionScript, ConditionalAI, TGCondition


class _Spy(ConditionScript):
    """A ConditionScript with a hand-installed instance, so the test does not
    depend on any particular SDK condition module being importable."""
    def __init__(self):
        super().__init__()
        self.activated = 0
        self.deactivated = 0

        outer = self

        class _Inst:
            def Activate(self):
                outer.activated += 1

            def Deactivate(self):
                outer.deactivated += 1

        self._instance = _Inst()


def test_set_active_calls_the_scripts_activate():
    cond = _Spy()
    cond.SetActive()
    assert cond.activated == 1
    assert cond.IsActive() == 1

    cond.SetInactive()
    assert cond.deactivated == 1
    assert cond.IsActive() == 0


def test_conditional_ai_activates_the_conditions_it_is_given():
    """A condition is activated when it is wired (the original's polled
    conditions evaluate in branches it never runs), and again on each node
    activation -- that second call is what re-arms a ConditionTimer (see
    tests/unit/test_ai_activation_lifecycle.py)."""
    cond = _Spy()
    ai = ConditionalAI(None, "gate")
    ai.AddCondition(cond)
    assert cond.activated == 1, "wiring activates the condition"

    ai.SetActive()   # node becomes active in the tree
    assert cond.activated == 2, "node activation calls Activate() again"


def test_plain_tgcondition_without_an_instance_is_unaffected():
    cond = TGCondition()
    cond.SetActive()          # must not raise
    assert cond.IsActive() == 1
