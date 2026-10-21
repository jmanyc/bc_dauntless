"""A ship's AI tree is freed with the ship, as in the original.

BC's AI tree is C++: deleting a ship deletes it, each ConditionScript destructor
drops its Python script instance, and the script's __del__ releases what it
holds. ConditionIncomingTorps keeps a module-wide registry keyed by ship NAMES
(g_dInternalInstances) and leaves it in __del__. Dauntless never tore a tree
down, so the next fight with the same names -- Quick Battle reuses them --
inherited a stale torpedo watcher bound to the dead ships and torpedo evasion
never fired again (engine/appc/ai.py destroy_ai_tree).
"""
import App
from engine.appc.ai import ConditionalAI_Create, ConditionScript_Create, destroy_ai_tree


def _incoming_registry():
    import Conditions.ConditionIncomingTorps as M
    return M.g_dInternalInstances


def test_destroying_a_tree_runs_each_condition_teardown_once():
    reg = _incoming_registry()           # never cleared: other tests' conditions still own entries
    key = ("TreeTest-Player", "TreeTest-Enemy-1")
    cond = ConditionScript_Create("Conditions.ConditionIncomingTorps", "ConditionIncomingTorps", *key)
    node = ConditionalAI_Create(None, "IncomingTorps")
    node.AddCondition(cond)
    assert key in reg
    destroy_ai_tree(node)
    assert key not in reg, "the condition released its registry entry"
    destroy_ai_tree(node)                      # a second teardown is a no-op
    import gc
    gc.collect()                               # nor does a later collection run __del__ again


def test_deleting_a_set_frees_its_ships_trees():
    reg = _incoming_registry()
    from engine.appc.sets import SetClass
    pSet = App.SetClass_Create() if hasattr(App, "SetClass_Create") else SetClass()

    class _Ship:
        pass
    ship = _Ship()
    cond = ConditionScript_Create("Conditions.ConditionIncomingTorps", "ConditionIncomingTorps", "TreeTest-Set-Player", None)
    node = ConditionalAI_Create(None, "IncomingTorps")
    node.AddCondition(cond)
    ship._ai = node
    pSet._objects["TreeTest-Set-Player"] = ship
    App.g_kSetManager._sets["__t"] = pSet
    App.g_kSetManager.DeleteSet("__t")
    assert ("TreeTest-Set-Player", None) not in reg
    assert ship._ai is None


def _tree_with_watcher(name):
    cond = ConditionScript_Create("Conditions.ConditionIncomingTorps", "ConditionIncomingTorps", name, None)
    node = ConditionalAI_Create(None, "IncomingTorps")
    node.AddCondition(cond)
    return node


def test_clear_ai_deletes_the_tree_unless_told_to_keep_it():
    """HelmMenuHandlers.py:2323-2325: 'The 0 here tells the game not to delete
    the old AI' -- pShip.ClearAI(0, pOldAI). A bare ClearAI() deletes it (the
    AI driver's release of a finished tree, MissionLib.SetPlayerAI)."""
    from engine.appc.ships import ShipClass
    reg = _incoming_registry()
    ship = ShipClass()
    ship.SetAI(_tree_with_watcher("TreeTest-Clear-A"))
    ship.ClearAI()
    assert ("TreeTest-Clear-A", None) not in reg
    kept = _tree_with_watcher("TreeTest-Clear-B")
    ship.SetAI(kept)
    ship.ClearAI(0, kept)                      # the fleet-command override keeps it for reuse
    assert ("TreeTest-Clear-B", None) in reg
    destroy_ai_tree(kept)


def test_set_ai_deletes_the_outgoing_tree_unless_told_to_keep_it():
    from engine.appc.ships import ShipClass
    reg = _incoming_registry()
    ship = ShipClass()
    ship.SetAI(_tree_with_watcher("TreeTest-Set-A"))
    ship.SetAI(_tree_with_watcher("TreeTest-Set-B"))
    assert ("TreeTest-Set-A", None) not in reg
    ship.SetAI(_tree_with_watcher("TreeTest-Set-C"), 0, 0)
    assert ("TreeTest-Set-B", None) in reg, "SetAI(ai, 0, 0) keeps the old tree"
