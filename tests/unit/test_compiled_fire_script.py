"""The compiled FireScript node (cleanroom/spec/01_FireScript_compiled.md).

Bound through the preprocessor registry as a mixin ahead of the SDK FireScript,
inside the non-lethal wrapper. Updates every 0.1 s, alternating an aim tick
(visibility, then target subsystem) and a fire tick (every weapon system once).
"""
import math

import pytest

import App
from engine.appc import optimized_fire_script as ofs
from engine.appc.ai import PreprocessingAI_Create
from engine.appc.math import TGPoint3
from engine.appc.ships import ShipClass
from engine.appc.subsystems import (
    HullSubsystem, PhaserSystem, ShieldSubsystem, TorpedoSystem,
)

PS_NORMAL = App.PreprocessingAI.PS_NORMAL
PS_DONE = App.PreprocessingAI.PS_DONE


def _reset_app_state():
    App.g_kSetManager._sets.clear()
    if hasattr(App.g_kEventManager, "_method_handlers"):
        App.g_kEventManager._method_handlers.clear()


@pytest.fixture(autouse=True)
def _isolate():
    _reset_app_state()
    yield
    _reset_app_state()


@pytest.fixture
def clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ofs, "_now", lambda: t[0])
    return t


def _scene(target_y=100.0):
    pSet = App.SetClass_Create(); pSet.SetName("S")
    App.g_kSetManager._sets["S"] = pSet
    ours = ShipClass(); ours.SetTranslateXYZ(0, 0, 0)
    ours._hull = HullSubsystem("H")
    ours._hull.SetMaxCondition(1000.0)
    pSet.AddObjectToSet(ours, "Ours")
    target = ShipClass(); target.SetTranslateXYZ(0, target_y, 0)
    target._hull = HullSubsystem("H")
    target._hull.SetMaxCondition(1000.0)
    pSet.AddObjectToSet(target, "Target")
    return ours, target


class _Calls:
    def __init__(self):
        self.log = []


def _instrument(system, calls, name):
    system.StartFiring = lambda t, o: calls.log.append(("start", name, (o.x, o.y, o.z)))
    system.StopFiringAtTarget = lambda t: calls.log.append(("stop", name))
    return system


def _bind(ours, *weapons, **fields):
    from AI.Preprocessors import FireScript
    sdk = FireScript("Target")
    for w in weapons:
        sdk.AddWeaponSystem(w)
    for k, v in fields.items():
        setattr(sdk, k, v)
    node = PreprocessingAI_Create(ours, "Fire")
    node.SetPreprocessingMethod(sdk, "Update")
    return node.GetPreprocessingInstance()


def _phasers(name, ours, calls, R=None, at=(0, 0, 0)):
    p = PhaserSystem(name)
    p._parent_ship = ours
    _instrument(p, calls, name)
    p.GetWorldLocation = lambda: TGPoint3(*at)
    if R is not None:
        bank = type("Bank", (), {"GetMaxDamageDistance": lambda self: R})()
        p.GetNumChildSubsystems = lambda: 1
        p.GetChildSubsystem = lambda i: bank
    return p


# ── Binding ──────────────────────────────────────────────────────────────────

def test_binding_is_the_compiled_mixin_ahead_of_the_sdk_class():
    from AI.Preprocessors import FireScript
    ours, _t = _scene()
    inst = _bind(ours)
    mro = type(inst).__mro__
    assert isinstance(inst, ofs.CompiledFireScriptMixin)
    assert mro.index(ofs.CompiledFireScriptMixin) < mro.index(FireScript)
    assert inst.GetNextUpdateTime() == 0.1


def test_no_target_reports_done_but_the_wrapper_keeps_it_non_lethal():
    ours, _t = _scene()
    inst = _bind(ours, sTarget="Nobody")
    assert ofs.CompiledFireScriptMixin.Update(inst, 0.0) == PS_DONE
    assert inst.Update(0.0) == PS_NORMAL


# ── Cadence ──────────────────────────────────────────────────────────────────

def test_aim_then_fire_every_system_once_per_fire_tick(clock):
    ours, _t = _scene()
    calls = _Calls()
    p1, p2 = _phasers("P1", ours, calls), _phasers("P2", ours, calls)
    inst = _bind(ours, p1, p2)
    inst.Update(0.0)                       # aim tick
    assert [c for c in calls.log if c[0] == "start"] == []
    inst.Update(0.0)                       # fire tick: BOTH systems
    assert [c[1] for c in calls.log if c[0] == "start"] == ["P1", "P2"]
    calls.log.clear()
    inst.Update(0.0)                       # aim
    inst.Update(0.0)                       # fire
    assert [c[1] for c in calls.log if c[0] == "start"] == ["P1", "P2"]


def test_aim_tick_chooses_the_target_subsystem(clock):
    ours, _t = _scene()
    calls = _Calls()
    inst = _bind(ours, _phasers("P", ours, calls))
    chosen = []
    inst.ChooseTargetSubsystem = lambda t: chosen.append(t)
    inst.Update(0.0)
    assert len(chosen) == 1
    inst.Update(0.0)                       # fire tick does not choose
    assert len(chosen) == 1


def test_invisible_target_stops_firing_and_stays_at_aim(clock):
    ours, _t = _scene(target_y=100.0)
    calls = _Calls()
    inst = _bind(ours, _phasers("P", ours, calls), fMaxFiringRange=50.0)
    for _ in range(3):
        inst.Update(0.0)
    assert ("stop", "P") in calls.log
    assert not any(c[0] == "start" for c in calls.log)
    assert inst.__dict__["_ofs_fire_tick"] is False
    inst.fMaxFiringRange = 1.0e20           # now in range: the next update aims
    inst.Update(0.0)
    assert inst.__dict__["_ofs_fire_tick"] is True


def test_disabled_or_empty_runs_contained_ai_without_firing(clock):
    ours, _t = _scene()
    calls = _Calls()
    inst = _bind(ours, _phasers("P", ours, calls), bEnabled=0)
    for _ in range(4):
        assert inst.Update(0.0) == PS_NORMAL
    assert calls.log == []
    empty = _bind(ours)
    assert empty.Update(0.0) == PS_NORMAL


# ── Weapon-type broadcast ────────────────────────────────────────────────────

def test_weapon_type_broadcast_calls_tree_and_clears_flag(clock):
    ours, _t = _scene()
    calls = _Calls()
    p = _phasers("P", ours, calls)
    inst = _bind(ours, p)
    seen = []

    class _AI:
        def CallExternalFunction(self, name, arg):
            seen.append((name, list(arg)))
    inst.pCodeAI.GetAllAIsInTree = lambda: [inst.pCodeAI, _AI()]
    inst.bCallUsingWeaponTypeFunc = 1
    inst.Update(0.0)
    assert seen == [("UsingWeaponType", [p])]
    assert inst.bCallUsingWeaponTypeFunc == 0


def test_too_dangerous_torpedoes_are_excluded_then_restored(clock):
    ours, _t = _scene()
    calls = _Calls()
    p = _phasers("P", ours, calls)
    t = TorpedoSystem("T"); t._parent_ship = ours
    _instrument(t, calls, "T")
    inst = _bind(ours, p, t)
    dangerous = [True]
    inst.WeaponTooDangerous = lambda w, tg, s: dangerous[0] and w is t
    inst.bCallUsingWeaponTypeFunc = 0
    inst.Update(0.0); inst.Update(0.0)
    assert ("stop", "T") in calls.log, "configure failed -> stop THIS system"
    assert inst.__dict__["_ofs_no_torps"] is True
    assert inst.bCallUsingWeaponTypeFunc == 1
    assert inst._broadcast_weapons() == [p], "torpedoes left out of the broadcast"

    seen = []

    class _AI:
        def CallExternalFunction(self, name, arg):
            seen.append(list(arg))
    inst.pCodeAI.GetAllAIsInTree = lambda: [inst.pCodeAI, _AI()]
    dangerous[0] = False
    inst.Update(0.0)                       # aim tick: broadcast without torps
    assert seen == [[p]]
    inst.Update(0.0)                       # fire tick: torps back in
    assert inst.__dict__["_ofs_no_torps"] is False
    assert inst.bCallUsingWeaponTypeFunc == 1


# ── Fire tick details ────────────────────────────────────────────────────────

def test_no_subsystem_and_no_attack_without_subsystems_stops_each(clock):
    ours, _t = _scene()
    calls = _Calls()
    inst = _bind(ours, _phasers("A", ours, calls), _phasers("B", ours, calls),
                 bAttackIfNoSubsystemsLeft=0)
    inst.Update(0.0); inst.Update(0.0)
    assert ("stop", "A") in calls.log and ("stop", "B") in calls.log
    assert not any(c[0] == "start" for c in calls.log)


def test_bad_shot_stops_this_system(clock):
    ours, _t = _scene()
    calls = _Calls()
    inst = _bind(ours, _phasers("P", ours, calls))
    inst.CheckGoodShot = lambda w, t, s: 0
    inst.Update(0.0); inst.Update(0.0)
    assert calls.log == [("stop", "P")]


def test_start_firing_uses_subsystem_position_as_offset(clock):
    ours, target = _scene()
    calls = _Calls()
    inst = _bind(ours, _phasers("P", ours, calls))
    sub = HullSubsystem("Bridge")
    sub.GetPositionTG = lambda: TGPoint3(1.0, 2.0, 3.0)
    sub.GetWorldLocation = lambda: TGPoint3(1.0, 102.0, 3.0)
    inst.FireSystemAtTarget(inst.lWeapons[0], target, sub)
    assert calls.log == [("start", "P", (1.0, 2.0, 3.0))]
    calls.log.clear()
    inst.FireSystemAtTarget(inst.lWeapons[0], target, None)
    assert calls.log == [("start", "P", (0.0, 0.0, 0.0))]


def test_dumb_fire_fires_only_tubes_facing_the_aim_point(clock, monkeypatch):
    ours, target = _scene()
    calls = _Calls()
    t = TorpedoSystem("T"); t._parent_ship = ours
    _instrument(t, calls, "T")
    t.GetWorldLocation = lambda: TGPoint3(0, 0, 0)

    class _Tube:
        def __init__(self, d):
            self.d, self.fired = d, 0
        def CalculateRoughDirection(self):
            return TGPoint3(*self.d)
        def FireDumb(self, a, b):
            self.fired += 1
    fwd, aft = _Tube((0, 1, 0)), _Tube((0, -1, 0))
    t.GetNumChildSubsystems = lambda: 2
    t.GetChildSubsystem = lambda i: (fwd, aft)[i]
    monkeypatch.setattr(App, "TorpedoTube_Cast", lambda o: o)
    inst = _bind(ours, t, bDumbFireTorps=1)
    inst.CheckGoodShot = lambda w, tg, s: 1
    inst.FireSystemAtTarget(t, target, None)
    assert (fwd.fired, aft.fired) == (1, 0)
    assert calls.log == [], "dumb-fire does nothing else"


# ── Configure: phaser power ──────────────────────────────────────────────────

@pytest.mark.parametrize("change,high,expected", [
    (1, 1, "PP_HIGH"), (1, 0, "PP_LOW"), (0, 1, None), (0, 0, None)])
def test_phaser_power_set_only_when_change_power_setting(clock, change, high, expected):
    ours, target = _scene()
    calls = _Calls()
    p = _phasers("P", ours, calls)
    levels = []
    p.SetPowerLevel = lambda lvl: levels.append(lvl)
    inst = _bind(ours, p, bChangePowerSetting=change, bHighPower=high)
    assert inst.ConfigureWeaponSystem(p, target, None) == 1
    assert levels == ([] if expected is None else [getattr(App.PhaserSystem, expected)])


# ── The smart-phaser rule ────────────────────────────────────────────────────

def test_rule_bands():
    R = 10.0
    assert ofs.smart_phaser_rule(19.9, R, 0.0, 0.0) is True     # < 2R: fire
    assert ofs.smart_phaser_rule(60.1, R, 1.0, 1.0) is False    # > 6R: hold
    # Between: x = (6R - d) / 4R; fire when s + acc + x > 1.75.
    assert ofs.smart_phaser_rule(20.0, R, 1.0, 0.0) is True     # x = 1
    assert ofs.smart_phaser_rule(30.0, R, 1.0, 0.0) is False    # x = 0.75: 1.75, not >
    assert ofs.smart_phaser_rule(30.0, R, 1.0, 0.01) is True
    assert ofs.smart_phaser_rule(60.0, R, 1.0, 0.76) is True    # x = 0
    assert ofs.smart_phaser_rule(60.0, R, 1.0, 0.75) is False


def test_rule_uses_first_bank_range_and_distance_to_aim_point(clock):
    ours, target = _scene(target_y=100.0)
    calls = _Calls()
    p = _phasers("P", ours, calls, R=60.0)
    inst = _bind(ours, p, bSmartPhasers=1)
    assert inst._smart_phaser_shot(p, target, None) is True     # 100 < 120
    p2 = _phasers("P2", ours, calls, R=15.0)
    assert inst._smart_phaser_shot(p2, target, None) is False   # 100 > 90


def test_no_banks_means_R_is_one(clock):
    ours, target = _scene(target_y=1.5)
    calls = _Calls()
    p = _phasers("P", ours, calls)
    p.GetNumChildSubsystems = lambda: 0
    inst = _bind(ours, p, bSmartPhasers=1)
    assert inst._smart_phaser_shot(p, target, None) is True     # 1.5 < 2


def test_accumulator_rises_and_falls_with_elapsed_time(clock):
    ours, target = _scene(target_y=100.0)
    calls = _Calls()
    clock[0] = 10.0
    near = _phasers("Near", ours, calls, R=60.0)      # passes
    far = _phasers("Far", ours, calls, R=10.0)        # 100 > 60: fails
    inst = _bind(ours, near, bSmartPhasers=1)
    assert inst._ofs_acc == 0.0 and inst.__dict__["_ofs_clock"] == 10.0
    clock[0] = 14.0
    inst._smart_phaser_shot(near, target, None)
    assert inst._ofs_acc == pytest.approx(0.2)        # 4 s x 0.05
    clock[0] = 18.0
    inst._smart_phaser_shot(far, target, None)
    assert inst._ofs_acc == pytest.approx(0.1)        # 4 s x 0.025
    clock[0] = 100.0
    inst._smart_phaser_shot(far, target, None)
    assert inst._ofs_acc == 0.0                       # floored
    clock[0] = 200.0
    inst._smart_phaser_shot(near, target, None)
    assert inst._ofs_acc == 1.0                       # capped


def test_accumulator_lets_a_held_shot_through_later(clock):
    """x = 0.5, s = 1: needs acc > 0.25, i.e. more than 5 s of passing."""
    ours, target = _scene(target_y=40.0)
    calls = _Calls()
    p = _phasers("P", ours, calls, R=10.0)            # d = 40 = 4R -> x 0.5
    inst = _bind(ours, p, bSmartPhasers=1)
    assert inst._smart_phaser_shot(p, target, None) is False
    inst.__dict__["_ofs_acc"] = 0.26
    assert inst._smart_phaser_shot(p, target, None) is True


def test_configure_with_smart_phasers_returns_the_rule(clock):
    ours, target = _scene(target_y=100.0)
    calls = _Calls()
    p = _phasers("P", ours, calls, R=10.0)
    p.SetPowerLevel = lambda lvl: None
    inst = _bind(ours, p, bSmartPhasers=1)
    assert inst.ConfigureWeaponSystem(p, target, None) == 0
    inst.Update(0.0); inst.Update(0.0)
    assert ("stop", "P") in calls.log


# ── Shield factor s ──────────────────────────────────────────────────────────

def _shielded_target(target, strengths, on=True):
    sh = ShieldSubsystem("Shields")
    if on:
        sh.TurnOn()
    else:
        sh.TurnOff()
    sh.GetCurShields = lambda i: strengths[i]
    target.SetShieldSubsystem(sh)
    return sh


def test_shield_factor_strong_facing_face(clock):
    from engine.appc.combat import _shield_face_from_hit_point
    ours, target = _scene()
    face = _shield_face_from_hit_point(target, ours.GetWorldLocation())
    strengths = [100.0] * 6
    strengths[face] = 400.0
    _shielded_target(target, strengths)
    s = ofs._shield_strength_factor(target, ours)
    assert s == pytest.approx(math.atan(100.0 / 400.0) / (math.pi / 2))


def test_shield_factor_is_one_when_face_not_stronger_than_average(clock):
    from engine.appc.combat import _shield_face_from_hit_point
    ours, target = _scene()
    face = _shield_face_from_hit_point(target, ours.GetWorldLocation())
    strengths = [100.0] * 6
    strengths[face] = 100.0
    _shielded_target(target, strengths)
    assert ofs._shield_strength_factor(target, ours) == 1.0


def test_shield_factor_is_one_with_shields_off(clock):
    from engine.appc.combat import _shield_face_from_hit_point
    ours, target = _scene()
    face = _shield_face_from_hit_point(target, ours.GetWorldLocation())
    strengths = [100.0] * 6
    strengths[face] = 400.0
    _shielded_target(target, strengths, on=False)
    assert ofs._shield_strength_factor(target, ours) == 1.0
