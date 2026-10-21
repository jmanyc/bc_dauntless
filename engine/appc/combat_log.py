"""Opt-in per-hit combat log for parity work (DAUNTLESS_COMBAT_LOG=1).

One JSON line per weapon hit applied by combat.apply_hit, written to
logs/combat_<launch time>.jsonl: game time, the two ships, weapon type, the
damage and its split (shields absorbed / leaked / hull), the shield face hit
and every face's level after the hit, the hit point in the target's body
frame, the damage reach, and each targetable subsystem in the footprint with
its condition after the hit. Compared offline with the original game's
CombatTap records (stbc-oracle) to see why subsystem damage feels different
in a real fight. Off by default; nothing here runs unless the variable is set.
"""
from __future__ import annotations

import json
import os
import time

_path = None
_enabled = None


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("DAUNTLESS_COMBAT_LOG", "") not in ("", "0")
    return _enabled


def _out():
    global _path
    if _path is None:
        from engine.paths import PROJECT_ROOT  # noqa: WPS433 - resolved at use
        d = os.path.join(str(PROJECT_ROOT), "logs")
        os.makedirs(d, exist_ok=True)
        _path = os.path.join(d, time.strftime("combat_%Y%m%d_%H%M%S.jsonl"))
    return _path


def _name(obj):
    try:
        return obj.GetName() if obj is not None else None
    except Exception:  # noqa: BLE001
        return None


def _body(ship, p):
    try:
        w = ship.GetWorldLocation()
        R = ship.GetWorldRotation()
        d = (p.x - w.x, p.y - w.y, p.z - w.z)
        return [round(R.GetCol(i).x * d[0] + R.GetCol(i).y * d[1] + R.GetCol(i).z * d[2], 3) for i in range(3)]
    except Exception:  # noqa: BLE001
        return None


def hit(ship, source, *, weapon_type, damage, face, absorbed_shields, post_shield,
        absorbed_hull, r_hit, hit_point, damage_point, allocations, overflow_depth,
        r_hull=None) -> None:
    """Record one applied hit. Never raises."""
    if not enabled():
        return
    try:
        import App
        t = App.g_kUtopiaModule.GetGameTime()
    except Exception:  # noqa: BLE001
        t = None
    try:
        faces = None
        sh = ship.GetShields() if hasattr(ship, "GetShields") else None
        if sh is not None and hasattr(sh, "GetCurrentShields"):
            faces = [round(sh.GetCurrentShields(i), 1) for i in range(6)]
        hull = ship.GetHull() if hasattr(ship, "GetHull") else None
        subs = []
        for sub, w in allocations or ():
            try:
                subs.append([_name(sub), round(sub.GetCondition(), 1), round(sub.GetMaxCondition(), 1)])
            except Exception:  # noqa: BLE001
                pass
        rec = {
            "t": t, "target": _name(ship), "source": _name(source), "weapon": weapon_type,
            "dmg": round(float(damage), 2), "face": face, "absorbed": round(float(absorbed_shields), 2),
            "leaked": round(float(post_shield), 2), "hull_dmg": round(float(absorbed_hull), 2),
            "r": None if r_hit is None else round(float(r_hit), 3),
            "r_hull": None if r_hull is None else round(float(r_hull), 4),
            "pos": _body(ship, hit_point),
            "dmg_pos": _body(ship, damage_point) if damage_point is not None else None,
            "faces_after": faces,
            "hull_after": None if hull is None else round(hull.GetCondition(), 1),
            "subs": subs, "overflow": int(overflow_depth),
        }
        with open(_out(), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass
