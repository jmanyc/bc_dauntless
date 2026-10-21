"""The compiled FireScript node, as the shipped game runs it.

The SDK's ``AI.Preprocessors.FireScript`` is only set-up code: once the AI
holding it is built, the engine copies the Python instance's settings and runs
its own node, and the Python ``Update`` is never called in play. This module is
that node, written from cleanroom/spec/01_FireScript_compiled.md.

``CompiledFireScriptMixin`` sits AHEAD of the SDK FireScript class in the
binding the registry builds (engine/appc/ai_optimized.py), so it overrides
``Update``, ``GetNextUpdateTime`` and ``FireSystemAtTarget`` and reuses every
SDK helper -- ``TargetInRange``, ``TargetVisible``, ``ChooseTargetSubsystem``,
``CheckGoodShot``, ``WeaponTooDangerous``, ``StopFiring`` and the tractor and
torpedo-type parts of ``ConfigureWeaponSystem`` -- with their SDK behaviour.
The settings it reads are the SDK instance's own fields (``lWeapons``,
``bEnabled``, ``idTargetedSubsystem``, ``bAttackIfNoSubsystemsLeft``,
``bChangePowerSetting``, ``bHighPower``, ``bSmartPhasers``, ``bDumbFireTorps``,
``bCallUsingWeaponTypeFunc``).

Node state of its own, all in the shared ``__dict__``:

* ``_ofs_acc``    -- the smart-phaser accumulator, 0..1, starting at 0;
* ``_ofs_clock``  -- game time of the accumulator's last update (creation time
                     at first);
* ``_ofs_fire_tick`` -- whether the next update is a fire tick (else aim);
* ``_ofs_no_torps``  -- torpedoes excluded as too dangerous.

engine/combat_recorder.py instruments ``FireSystemAtTarget`` and
``_smart_phaser_shot`` and reads ``_ofs_acc``; keep those names.
"""

import math

import App

# The node asks to be updated every 0.1 s, alternating aim and fire ticks, so
# each weapon system is re-decided every 0.2 s however many there are.
UPDATE_INTERVAL_S = 0.1

# Smart phasers: always fire inside 2R, never beyond 6R (R = first bank's
# maximum damage distance); in between fire when s + accumulator + x > 1.75.
SMART_FIRE_INSIDE_R = 2.0
SMART_HOLD_BEYOND_R = 6.0
SMART_THRESHOLD = 1.75
# Accumulator: +0.05/s while the rule passes (cap 1), -0.025/s while it fails
# (floor 0).
ACC_RISE_PER_S = 0.05
ACC_FALL_PER_S = 0.025


def _now() -> float:
    try:
        return float(App.g_kUtopiaModule.GetGameTime())
    except Exception:
        return 0.0


def _dist(a, b) -> float:
    """Distance between two points (TGPoint3-like)."""
    dx, dy, dz = a.x - b.x, a.y - b.y, a.z - b.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def init_node_state(state: dict) -> None:
    """Give a freshly bound node its own state (idempotent): accumulator 0,
    its clock at the creation time, the next tick an aim tick."""
    state.setdefault("_ofs_acc", 0.0)
    state.setdefault("_ofs_clock", _now())
    state.setdefault("_ofs_fire_tick", False)
    state.setdefault("_ofs_no_torps", False)


def _shield_strength_factor(target, shooter_ship) -> float:
    """s in the smart-phaser rule: 1, unless the target ship's shields are on
    and not disabled and the face toward the shooter is stronger than the
    average of the other five; then atan(avg / face) / (pi / 2)."""
    ship = App.ShipClass_Cast(target)
    if ship is None or shooter_ship is None:
        return 1.0
    shields = ship.GetShieldSubsystem()
    if shields is None or not shields.IsOn() or shields.IsDisabled():
        return 1.0
    from engine.appc.combat import _shield_face_from_hit_point
    face = int(_shield_face_from_hit_point(ship, shooter_ship.GetWorldLocation()))
    n = 6
    strengths = [float(shields.GetCurShields(i)) for i in range(n)]
    facing = strengths[face]
    avg = (sum(strengths) - facing) / 5.0
    if facing > avg:
        return math.atan(avg / facing) / (math.pi / 2.0)
    return 1.0


def smart_phaser_rule(d: float, R: float, s: float, acc: float) -> bool:
    """The smart-phaser decision for distance d, first-bank range R, shield
    factor s and accumulator acc."""
    if d < SMART_FIRE_INSIDE_R * R:
        return True
    if d > SMART_HOLD_BEYOND_R * R or R <= 0.0:
        return False
    x = (SMART_HOLD_BEYOND_R * R - d) / (4.0 * R)
    return s + acc + x > SMART_THRESHOLD


class CompiledFireScriptMixin:
    """The engine FireScript node; mixed in ahead of the SDK FireScript."""

    def GetNextUpdateTime(self):
        return UPDATE_INTERVAL_S

    # ── One update ─────────────────────────────────────────────────────────
    def Update(self, dEndTime=None):
        state = self.__dict__
        init_node_state(state)
        code_ai = state.get("pCodeAI")
        ship = code_ai.GetShip() if code_ai is not None else None
        if ship is None:
            return App.PreprocessingAI.PS_DONE
        pTarget = self.GetTarget()
        if not pTarget:
            return App.PreprocessingAI.PS_DONE

        if self.bCallUsingWeaponTypeFunc:
            lWeapons = self._broadcast_weapons()
            for pAI in code_ai.GetAllAIsInTree()[1:]:
                pAI.CallExternalFunction("UsingWeaponType", lWeapons)
            self.bCallUsingWeaponTypeFunc = 0

        if not self.bEnabled or not self.lWeapons:
            return App.PreprocessingAI.PS_NORMAL

        fire_tick = bool(state.get("_ofs_fire_tick"))
        if not fire_tick or not self.bTargetVisible:
            visible = bool(self.TargetInRange(pTarget)) and bool(self.TargetVisible(pTarget))
            self.bTargetVisible = 1 if visible else 0
            if not visible:
                self.StopFiring()
                state["_ofs_fire_tick"] = False
                return App.PreprocessingAI.PS_NORMAL

        if not fire_tick:
            self.ChooseTargetSubsystem(pTarget)
            state["_ofs_fire_tick"] = True
            return App.PreprocessingAI.PS_NORMAL

        pSubsystem = None
        if self.idTargetedSubsystem is not None:
            pSubsystem = App.ShipSubsystem_Cast(
                App.TGObject_GetTGObjectPtr(self.idTargetedSubsystem))
        for pWeaponSystem in list(self.lWeapons):
            if pSubsystem is None and not self.bAttackIfNoSubsystemsLeft:
                pWeaponSystem.StopFiringAtTarget(pTarget)
            else:
                self.FireSystemAtTarget(pWeaponSystem, pTarget, pSubsystem)
        state["_ofs_fire_tick"] = False
        return App.PreprocessingAI.PS_NORMAL

    def _broadcast_weapons(self) -> list:
        """The weapon list passed to UsingWeaponType: torpedo systems left out
        while torpedoes are excluded."""
        if not self.__dict__.get("_ofs_no_torps"):
            return self.lWeapons
        return [w for w in self.lWeapons if App.TorpedoSystem_Cast(w) is None]

    # ── Firing one system ──────────────────────────────────────────────────
    def FireSystemAtTarget(self, pWeaponSystem, pTarget, pSubsystem):
        if not self.ConfigureWeaponSystem(pWeaponSystem, pTarget, pSubsystem):
            pWeaponSystem.StopFiringAtTarget(pTarget)
            return
        if not self.CheckGoodShot(pWeaponSystem, pTarget, pSubsystem):
            pWeaponSystem.StopFiringAtTarget(pTarget)
            return
        if self.bDumbFireTorps and App.TorpedoSystem_Cast(pWeaponSystem):
            aim = (pSubsystem or pTarget).GetWorldLocation()
            here = pWeaponSystem.GetWorldLocation()
            to_aim = App.TGPoint3()
            to_aim.SetXYZ(aim.x - here.x, aim.y - here.y, aim.z - here.z)
            for i in range(pWeaponSystem.GetNumChildSubsystems()):
                pTube = App.TorpedoTube_Cast(pWeaponSystem.GetChildSubsystem(i))
                if pTube is not None and to_aim.Dot(pTube.CalculateRoughDirection()) > 0:
                    pTube.FireDumb(0, 1)
            return
        vOffset = App.TGPoint3()
        if pSubsystem:
            vOffset.Set(pSubsystem.GetPositionTG())
        else:
            vOffset.SetXYZ(0, 0, 0)
        pWeaponSystem.StartFiring(pTarget, vOffset)

    # ── Configure ──────────────────────────────────────────────────────────
    def ConfigureWeaponSystem(self, pWeaponSystem, pTarget, pSubsystem):
        state = self.__dict__
        is_torp = App.TorpedoSystem_Cast(pWeaponSystem) is not None
        if self.WeaponTooDangerous(pWeaponSystem, pTarget, pSubsystem):
            if is_torp and not state.get("_ofs_no_torps"):
                state["_ofs_no_torps"] = True
                self.bCallUsingWeaponTypeFunc = 1
            return 0
        if is_torp and state.get("_ofs_no_torps"):
            state["_ofs_no_torps"] = False
            self.bCallUsingWeaponTypeFunc = 1

        pPhasers = App.PhaserSystem_Cast(pWeaponSystem)
        if pPhasers is not None and App.TractorBeamSystem_Cast(pWeaponSystem) is None:
            if self.bChangePowerSetting:
                pPhasers.SetPowerLevel(App.PhaserSystem.PP_HIGH if self.bHighPower
                                       else App.PhaserSystem.PP_LOW)
            if not self.bSmartPhasers:
                return 1
            return 1 if self._smart_phaser_shot(pPhasers, pTarget, pSubsystem) else 0

        # Tractor and torpedo systems: the SDK's own configuration.
        return super().ConfigureWeaponSystem(pWeaponSystem, pTarget, pSubsystem)

    def _smart_phaser_shot(self, pPhaserSystem, pTarget, pSubsystem) -> bool:
        """Apply the smart-phaser rule, then move the accumulator by the game
        time elapsed since its last update."""
        state = self.__dict__
        init_node_state(state)
        R = 1.0
        if pPhaserSystem.GetNumChildSubsystems() > 0:
            bank = pPhaserSystem.GetChildSubsystem(0)
            if bank is not None:
                R = float(bank.GetMaxDamageDistance())
        d = _dist((pSubsystem or pTarget).GetWorldLocation(),
                  pPhaserSystem.GetWorldLocation())
        acc = float(state["_ofs_acc"])
        if d < SMART_FIRE_INSIDE_R * R or d > SMART_HOLD_BEYOND_R * R:
            ok = smart_phaser_rule(d, R, 1.0, acc)
        else:
            code_ai = state.get("pCodeAI")
            shooter = code_ai.GetShip() if code_ai is not None else None
            ok = smart_phaser_rule(d, R, _shield_strength_factor(pTarget, shooter), acc)

        now = _now()
        dt = max(0.0, now - float(state["_ofs_clock"]))
        state["_ofs_clock"] = now
        if ok:
            state["_ofs_acc"] = min(1.0, acc + ACC_RISE_PER_S * dt)
        else:
            state["_ofs_acc"] = max(0.0, acc - ACC_FALL_PER_S * dt)
        return ok
