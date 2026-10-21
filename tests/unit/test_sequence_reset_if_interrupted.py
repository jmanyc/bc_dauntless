"""SequenceAI.SetResetIfInterrupted: a deactivated sequence restarts at its first child.

Spec: cleanroom/spec/05_SequenceAI_ResetIfInterrupted.md. The flag is read only on
deactivation. Deactivation deactivates the current child; with the flag set the
sequence then restores its remaining-loop count, has no current child (so it starts
again at its first child), and resets each child as it becomes current again, before
activating it. With the flag clear it resumes where it was.
"""
from engine.appc.ai import (
    ArtificialIntelligence, PlainAI_Create, PriorityListAI_Create, SequenceAI_Create,
)
from engine.appc.ai_driver import tick_ai

ACTIVE = ArtificialIntelligence.US_ACTIVE
DORMANT = ArtificialIntelligence.US_DORMANT
DONE = ArtificialIntelligence.US_DONE


class _Leaf:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.updates = 0
        self.resets = 0

    def GetNextUpdateTime(self):
        return 0.0

    def Update(self):
        self.updates += 1
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def Reset(self):
        self.resets += 1


def _leaf(name, *statuses):
    ai = PlainAI_Create(None, name)
    ai._script_instance = _Leaf(statuses or (ACTIVE,))
    return ai


def _two_step(flag):
    seq = SequenceAI_Create(None, "seq")
    seq.SetLoopCount(-1)
    seq.SetResetIfInterrupted(flag)
    first = _leaf("first", DONE)
    second = _leaf("second", ACTIVE)
    seq.AddAI(first)
    seq.AddAI(second)
    return seq, first, second


def _advance_to_second(seq):
    seq.SetActive()
    tick_ai(seq, 0.0)                      # first runs, answers DONE, cursor -> second
    tick_ai(seq, 0.1)                      # second runs
    assert seq._current_index == 1


def test_flag_set_restarts_at_first_child_after_deactivation():
    seq, first, second = _two_step(1)
    _advance_to_second(seq)
    second.SetActive()
    seq.SetInactive()
    assert not second._is_active_in_tree, "deactivation deactivates the current child"
    assert seq._current_index == 0

    before = first._script_instance.updates
    seq.SetActive()
    tick_ai(seq, 0.2)
    assert first._script_instance.updates == before + 1, "restarts at the FIRST child"
    assert first._script_instance.resets == 1, "the child is reset as it becomes current"


def test_each_child_is_reset_as_it_becomes_current_and_only_once():
    seq, first, second = _two_step(1)
    _advance_to_second(seq)
    seq.SetInactive()
    seq.SetActive()
    tick_ai(seq, 0.2)                      # first reset + run (DONE) -> cursor on second
    assert second._script_instance.resets == 0, "second is not current yet"
    tick_ai(seq, 0.3)
    assert second._script_instance.resets == 1
    tick_ai(seq, 0.4)
    assert second._script_instance.resets == 1, "reset once per rewind, not every tick"


def test_child_is_reset_before_it_is_read():
    """A child left DONE by the last pass is reset (re-armed) before the scan
    decides to skip it, so the rewound sequence really runs it again."""
    seq = SequenceAI_Create(None, "seq")
    seq.SetResetIfInterrupted(1)
    first = _leaf("first", DONE)
    second = _leaf("second", ACTIVE)
    seq.AddAI(first)
    seq.AddAI(second)
    _advance_to_second(seq)
    assert first._status == DONE
    seq.SetInactive()
    tick_ai(seq, 0.2)
    assert first._script_instance.updates == 2


def test_flag_set_restores_remaining_loop_count():
    seq = SequenceAI_Create(None, "seq")
    seq.SetLoopCount(3)
    seq.SetResetIfInterrupted(1)
    seq.AddAI(_leaf("only", DONE))
    seq.SetActive()
    tick_ai(seq, 0.0)
    assert seq._loops_remaining == 2
    seq.SetInactive()
    assert seq._loops_remaining == 3


def test_flag_clear_resumes_where_it_was():
    seq, first, second = _two_step(0)
    _advance_to_second(seq)
    seq.SetInactive()
    assert seq._current_index == 1
    before = first._script_instance.updates
    seq.SetActive()
    tick_ai(seq, 0.2)
    assert first._script_instance.updates == before
    assert first._script_instance.resets == 0
    assert second._script_instance.updates == 2


def test_flag_clear_still_deactivates_current_child():
    seq, first, second = _two_step(0)
    _advance_to_second(seq)
    second.SetActive()
    seq.SetInactive()
    assert not second._is_active_in_tree


def test_flag_is_read_only_on_deactivation():
    """Setting the flag changes nothing while the sequence keeps running."""
    seq, first, second = _two_step(0)
    _advance_to_second(seq)
    seq.SetResetIfInterrupted(1)
    tick_ai(seq, 0.2)
    assert seq._current_index == 1
    assert first._script_instance.resets == 0


def test_priority_list_interrupting_the_sequence_rewinds_it():
    """The CloakAttack case: a higher-priority child takes the list from the
    sequence; when it lets go the sequence restarts at its first child."""
    seq, first, second = _two_step(1)
    evade = _leaf("evade", DORMANT)
    plist = PriorityListAI_Create(None, "plist")
    plist.AddAI(evade, 1)
    plist.AddAI(seq, 2)

    tick_ai(plist, 0.0)
    tick_ai(plist, 0.1)
    assert seq._current_index == 1

    evade._script_instance.statuses = [ACTIVE]
    evade._status = ACTIVE
    tick_ai(plist, 0.2)                    # evade runs; the sequence leaves the active path
    assert not seq._is_active_in_tree
    assert seq._current_index == 0

    evade._script_instance.statuses = [DORMANT]
    first_updates = first._script_instance.updates
    for t in (0.3, 0.4):
        tick_ai(plist, t)
    assert first._script_instance.updates > first_updates
    assert first._script_instance.resets == 1
