"""The engine ManagePower node: budget and split.

Spec: cleanroom/spec/03_ManagePower_compiled.md. Every 3.0 s the node sets the
wanted power level of each powered system on its ship (not the power plant,
not the cloaking device), and always reports PS_NORMAL.
"""
import pytest

import App
from engine.appc import ai_optimized
from engine.appc.ai import PlainAI_Create, PreprocessingAI_Create
from engine.appc.ai_optimized import (
    ManagePower, manage_power_budget, manage_power_split, manage_power_systems,
)
from engine.appc.properties import PowerProperty
from engine.appc.subsystems import (
    CloakingSubsystem, ImpulseEngineSubsystem, PowerSubsystem, SensorSubsystem,
    ShieldSubsystem, WarpEngineSubsystem,
)
from engine.appc.weapon_subsystems import PhaserSystem, TractorBeamSystem


def _power(output=1000.0, limit=250000.0, backup_limit=80000.0,
           main_conduit=1200.0, backup_conduit=200.0):
    power = PowerSubsystem("Warp Core")
    prop = PowerProperty("Warp Core")
    prop.SetPowerOutput(output)
    prop.SetMainBatteryLimit(limit)
    prop.SetBackupBatteryLimit(backup_limit)
    prop.SetMainConduitCapacity(main_conduit)
    prop.SetBackupConduitCapacity(backup_conduit)
    power.SetProperty(prop)
    return power


def _on(system, normal):
    system.SetNormalPowerPerSecond(normal)
    system.TurnOn()
    return system


# ── The budget ───────────────────────────────────────────────────────────────

def test_no_battery_limit_budget_is_plant_output():
    power = _power(limit=0.0)
    assert manage_power_budget(power, False) == pytest.approx(1000.0)
    assert manage_power_budget(power, True) == pytest.approx(1000.0)


def test_full_battery_budget_is_conduit_supply():
    power = _power()                       # batteries spawn full
    S = 1200.0 + 200.0                     # min(A, main cap) + min(backup, backup cap)
    assert manage_power_budget(power, False) == pytest.approx(S)


def test_at_threshold_budget_is_plant_output():
    power = _power()
    power.SetMainBatteryPower(0.9 * 250000.0)
    assert manage_power_budget(power, False) == pytest.approx(1000.0)


@pytest.mark.parametrize("frac_of_T", [0.0, 0.25, 0.5, 0.99])
def test_below_threshold_budget_is_linear_in_battery(frac_of_T):
    power = _power()
    T = 0.9 * 250000.0
    power.SetMainBatteryPower(frac_of_T * T)
    # P x (0.1 + 0.9 A / T)
    assert manage_power_budget(power, False) == pytest.approx(
        1000.0 * (0.1 + 0.9 * frac_of_T))


def test_between_threshold_and_full_rises_to_supply():
    power = _power()
    L, T = 250000.0, 0.9 * 250000.0
    A = 0.95 * L
    power.SetMainBatteryPower(A)
    f = 1.0 - 0.5 * (L - A) / (L - T)
    S = min(A, 1200.0) + 200.0
    assert manage_power_budget(power, False) == pytest.approx(
        1000.0 * (2 - 2 * f) + (2 * f - 1) * S)


def test_conserving_full_battery_stays_below_output():
    power = _power()
    budget = manage_power_budget(power, True)
    assert budget == pytest.approx(1000.0 * (0.1 + 0.9 / 1.0625))
    assert budget == pytest.approx(947.06, abs=0.01)
    assert budget < 1000.0


def test_supply_term_uses_battery_limited_conduits():
    power = _power()
    power.SetBackupBatteryPower(50.0)      # backup below its conduit capacity
    S = 1200.0 + 50.0
    assert manage_power_budget(power, False) == pytest.approx(S)


def test_plant_output_scaled_by_condition():
    power = _power(limit=0.0)
    power.SetMaxCondition(100.0)
    power.SetCondition(50.0)
    assert manage_power_budget(power, False) == pytest.approx(500.0)


# ── The split ────────────────────────────────────────────────────────────────

def _levels(systems, budget):
    return {s.GetName(): lvl for s, lvl in manage_power_split(systems, budget)}


def test_weights_sensors_quarter_warp_twentieth_rest_one():
    a = _on(ShieldSubsystem("Shields"), 100.0)
    s = _on(SensorSubsystem("Sensors"), 100.0)
    w = _on(WarpEngineSubsystem("Warp"), 100.0)
    W = 1.0 + 0.25 + 0.05
    budget = 100.0                          # nobody reaches 1.25
    lv = _levels([a, s, w], budget)
    assert lv["Shields"] == pytest.approx(1.0 / W * budget / 100.0)
    assert lv["Sensors"] == pytest.approx(0.25 / W * budget / 100.0)
    assert lv["Warp"] == pytest.approx(0.05 / W * budget / 100.0)


def test_capped_system_leaves_split_and_rest_share_remainder():
    small = _on(SensorSubsystem("Sensors"), 10.0)       # wants little
    big = _on(ShieldSubsystem("Shields"), 1000.0)
    budget = 1000.0
    # Pass 1: W = 1.25; sensors share 200 -> level 20 -> capped at 1.25.
    # Pass 2: budget 1000 - 12.5, W = 1; shields 987.5 / 1000.
    lv = _levels([small, big], budget)
    assert lv["Sensors"] == 1.25
    assert lv["Shields"] == pytest.approx(0.9875)


def test_cap_repeats_until_nothing_is_capped():
    a = _on(ShieldSubsystem("A"), 10.0)
    b = _on(ImpulseEngineSubsystem("B"), 100.0)
    c = _on(PhaserSystem("C"), 1000.0)
    lv = _levels([a, b, c], 600.0)
    # Pass 1: 200 each -> A 20 (cap), B 2 (cap), C 0.2.
    # Pass 2: 600 - 12.5 - 125 = 462.5 to C alone -> 0.4625.
    assert lv["A"] == 1.25 and lv["B"] == 1.25
    assert lv["C"] == pytest.approx(0.4625)


def test_level_exactly_1_25_is_not_capped():
    a = _on(ShieldSubsystem("A"), 100.0)
    b = _on(ShieldSubsystem("B"), 100.0)
    lv = _levels([a, b], 250.0)
    assert lv["A"] == pytest.approx(1.25) and lv["B"] == pytest.approx(1.25)


def test_zero_demand_keeps_its_weight_and_its_share_is_lost():
    off = ShieldSubsystem("Off")
    off.SetNormalPowerPerSecond(100.0)
    off.TurnOff()
    on = _on(ShieldSubsystem("On"), 100.0)
    lv = _levels([off, on], 100.0)
    assert lv["Off"] == 0.0
    assert lv["On"] == pytest.approx(0.5), "half the budget is lost to the dead weight"


def test_disabled_system_has_no_demand():
    sys_ = _on(ShieldSubsystem("S"), 100.0)
    sys_.SetMaxCondition(100.0)
    sys_.SetDisabledPercentage(0.5)
    sys_.SetCondition(10.0)
    assert sys_.IsDisabled()
    assert _levels([sys_], 100.0)["S"] == 0.0


def test_tractor_not_firing_has_no_demand():
    t = _on(TractorBeamSystem("Tractor"), 100.0)
    assert not t.IsFiring()
    assert _levels([t], 100.0)["Tractor"] == 0.0


# ── Participants ─────────────────────────────────────────────────────────────

def _ship():
    ship = App.ShipClass_Create("TestShip")
    # Drop the placeholder systems ShipClass_Create attaches: each would keep
    # its weight in the split and make the expected levels fixture-dependent.
    ship._powered_consumers = []
    ship.SetPowerSubsystem(_power())
    return ship


def test_cloak_and_power_plant_never_take_part():
    ship = _ship()
    shields = _on(ShieldSubsystem("Shields"), 100.0)
    cloak = _on(CloakingSubsystem("Cloak"), 100.0)
    ship.SetShieldSubsystem(shields)
    ship.SetCloakingSubsystem(cloak)
    parts = manage_power_systems(ship)
    assert shields in parts
    assert cloak not in parts
    assert ship.GetPowerSubsystem() not in parts


# ── The node ─────────────────────────────────────────────────────────────────

def _bound_node(ship, conserve):
    import AI.Preprocessors
    node = PreprocessingAI_Create(ship, "PowerManagement")
    node.SetContainedAI(PlainAI_Create(ship, "child"))
    node.SetPreprocessingMethod(AI.Preprocessors.ManagePower(conserve), "Update")
    return node.GetPreprocessingInstance()


def test_node_sets_levels_and_leaves_cloak_alone():
    ship = _ship()
    shields = _on(ShieldSubsystem("Shields"), 100.0)
    sensors = _on(SensorSubsystem("Sensors"), 100.0)
    cloak = _on(CloakingSubsystem("Cloak"), 100.0)
    cloak.SetPowerPercentageWanted(0.7)
    for s in (shields, sensors):
        ship.AddPoweredConsumer(s)
    ship.SetCloakingSubsystem(cloak)

    inst = _bound_node(ship, 0)
    assert isinstance(inst, ManagePower)
    assert inst.GetNextUpdateTime() == 3.0
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL
    # Full battery: budget 1400; both cap at 1.25.
    assert shields.GetPowerPercentageWanted() == pytest.approx(1.25)
    assert sensors.GetPowerPercentageWanted() == pytest.approx(1.25)
    assert cloak.GetPowerPercentageWanted() == pytest.approx(0.7)


def test_node_conserve_flag_uses_conserve_budget():
    ship = _ship()
    shields = _on(ShieldSubsystem("Shields"), 2000.0)
    ship.AddPoweredConsumer(shields)
    inst = _bound_node(ship, 1)
    inst.Update(0.0)
    assert shields.GetPowerPercentageWanted() == pytest.approx(
        1000.0 * (0.1 + 0.9 / 1.0625) / 2000.0)


def test_node_never_returns_ps_done_without_a_ship():
    inst = ManagePower(0)
    assert inst.Update(0.0) == App.PreprocessingAI.PS_NORMAL
