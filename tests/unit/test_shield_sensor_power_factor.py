"""Sensor range scales by GetNormalPowerPercentage (power factor); shield regen does not."""
import pytest
from engine.appc.subsystems import SHIELD_REGEN_EFFICIENCY as E  # measured: authored rate x 0.843 (stbc-oracle reference §2.4)
from engine.appc.subsystems import ShieldSubsystem, SensorSubsystem


def test_shield_regen_scales_with_shields_power():
    """Regen scales linearly with the Shields power control: measured on the
    original exe 0 / 2.38 / 4.72 / 7.12 / 9.44 / 11.85 per s at 0-125%
    (stbc-oracle `power_effects`, `torp_reload_power`). The bible's S5 power
    line (`regen_power50_face50`) is superseded: that capture's own rows show
    the full rate, i.e. its 50% setting did not take effect. Without a bound
    power source the control setting itself is the factor."""
    ss = ShieldSubsystem("Shield Generator")
    ss.TurnOn()
    ss.SetMaxShields(ss.FRONT_SHIELDS, 100.0)
    ss.SetCurShields(ss.FRONT_SHIELDS, 0.0)
    ss.SetShieldChargePerSecond(ss.FRONT_SHIELDS, 10.0)
    ss.SetPowerPercentageWanted(0.5)
    ss.Update(1.0)
    assert ss.GetCurShields(ss.FRONT_SHIELDS) == pytest.approx(10.0 * E * 0.5)
    ss.SetPowerPercentageWanted(1.25)
    ss.Update(1.0)
    assert ss.GetCurShields(ss.FRONT_SHIELDS) == pytest.approx(10.0 * E * 0.5 + 10.0 * E * 1.25)


def test_sensor_range_scales_with_power_factor():
    from engine.appc import sensor_detection
    sen = SensorSubsystem("Sensor Array")
    sen.SetBaseSensorRange(100.0)
    sen._power_factor = 1.25

    class _Ship:
        def GetSensorSubsystem(self):
            return sen

    rng = sensor_detection.effective_sensor_range(_Ship())
    assert abs(rng - 125.0) < 1e-6


def test_shield_regen_at_full_power_unchanged():
    """At factor 1.0 regen is identical to the old formula (regression guard)."""
    ss = ShieldSubsystem("Shield Generator")
    ss.TurnOn()
    ss.SetMaxShields(ss.FRONT_SHIELDS, 100.0)
    ss.SetCurShields(ss.FRONT_SHIELDS, 0.0)
    ss.SetShieldChargePerSecond(ss.FRONT_SHIELDS, 10.0)
    # full power: the authored rate x the measured efficiency
    ss.Update(1.0)
    assert ss.GetCurShields(ss.FRONT_SHIELDS) == pytest.approx(10.0 * E)


def test_sensor_range_at_full_power_unchanged():
    """At factor 1.0 sensor range is identical to base * condition (regression guard)."""
    from engine.appc import sensor_detection
    sen = SensorSubsystem("Sensor Array")
    sen.SetBaseSensorRange(100.0)
    # _power_factor defaults to 1.0 — do not set it explicitly
    rng = sensor_detection.effective_sensor_range(type("_S", (), {"GetSensorSubsystem": lambda self: sen})())
    assert abs(rng - 100.0) < 1e-6
