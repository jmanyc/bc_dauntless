"""Live combat recorder for human-played fights (the Dauntless twin of the original's oracle duel telemetry).

Off unless DAUNTLESS_COMBAT_RECORD=<folder> is set. host_loop calls `tick(player)` once per frame; the first
call installs the hooks. A fight starts when another ship shares the player's set and ends TAIL_S after either
ship is dying (or the player changes); each fight is written to
<folder>/duel_<enemy>_enemy<lvl>_vs_<player>_player_human_<stamp>.json when it ends. Every FLUSH_S game
seconds the rows added since the last flush are appended to <same name>.part.jsonl, so a crash or an early
quit loses at most that much (`read_partial` rebuilds the document from it; the journal is deleted once the
full file is written). Rewriting the whole growing file instead stalled the frame for up to 0.3 s every 10 s
(dn_human_1, 12 MB by the end of an 8-minute fight).

The file has the fields stbc-oracle/scorecard/dauntless_aim_duels.py writes (track rows A/T poses, sA/sT
status, allhits, hitdetail, choices, beh, spd, tree/cond, fs_counts), so the matrix comparison scripts read it
unchanged. Roles follow the oracle duel: A = the enemy ("attacker"), T = the player ("target"). Additions for
human play: status `tg` (target name), `ts` (target subsystem), `al` (alert level), `vel` (speed, GU/s); and
`events`, every SetTarget / SetTargetSubsystem / SetAlertLevel / SetImpulse / SetSpeed the player's ship
receives, with game time.
"""
import atexit
import importlib
import json
import math
import os
import time

OUT_DIR = os.environ.get("DAUNTLESS_COMBAT_RECORD", "")
ENABLED = bool(OUT_DIR)
SAMPLE_DT = float(os.environ.get("DAUNTLESS_COMBAT_RECORD_DT", "0.1"))   # game seconds between track rows
FLUSH_S = 10.0
TAIL_S = 3.0

TREE_FACTORIES = ("ConditionalAI_Create", "PlainAI_Create", "PriorityListAI_Create", "SequenceAI_Create",
                  "PreprocessingAI_Create", "RandomAI_Create")
PLAIN_MODULES = ("TorpedoRun", "IntelligentCircleObject", "CircleObject", "ManeuverLoop", "Intercept",
                 "StationaryAttack", "FollowObject", "Flee", "PhaserSweep", "TurnToOrientation", "EvadeTorps",
                 "Warp", "MoveToObjectSide", "GoForward", "Stay")
PLAYER_EVENTS = ("SetTarget", "SetTargetSubsystem", "SetAlertLevel")

_installed = False
_nodes = []          # (node, ship name, kind, name) for every AI node built since install
_conds = []          # (condition, class name, arg, owner ship name)
_beh = {}            # (ship, PlainAI module) -> Update calls since the last row
_speed_last = {}     # ship -> [kind, value, script, calls] since the last row
_cur_script = ["-"]
_speed_depth = [0]
_fight = None        # the current _Fight, or None
_patched = []        # (owner, attribute, original) for uninstall()


def _now():
    import App
    return float(App.g_kUtopiaModule.GetGameTime())


def _name(o):
    try:
        return o.GetName()
    except Exception:
        return None


class _Fight:
    def __init__(self, player, enemy):
        self.player, self.enemy = player, enemy
        self.pname, self.ename = _name(player), _name(enemy)
        self.t0 = _now()
        self.next_sample = self.t0
        self.next_flush = self.t0 + FLUSH_S
        self.end_t = None
        self.winner_role = None
        self.node_mark = len(_nodes)
        self.cond_mark = len(_conds)
        self.track, self.allhits, self.hitdetail, self.choices, self.hits, self.events = [], [], [], [], [], []
        self.fs_counts = {"_smart": []}
        self.current = {}
        cls = lambda s: (s.GetScript() or "").split(".")[-1] if hasattr(s, "GetScript") else _name(s)
        self.eclass, self.pclass = cls(enemy), cls(player)
        self.level = None
        try:
            import sys
            qb = sys.modules.get("QuickBattle.QuickBattle")
            self.level = getattr(qb, "g_iCurrentAILevel", None) if qb is not None else None
        except Exception:
            pass
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(OUT_DIR, "duel_%s_enemy%s_vs_%s_player_human_%s.json" % (
            self.eclass, self.level if self.level is not None else "x", self.pclass, self.stamp))
        self.part = self.path[:-len(".json")] + ".part.jsonl"
        self.marks = {}      # list name -> rows already in the journal
        # roles start at the fight's first frame: re-mark nodes built for these two ships just before
        for i in range(len(_nodes) - 1, -1, -1):
            if _nodes[i][1] in (self.pname, self.ename):
                self.node_mark = min(self.node_mark, i)
            else:
                break
        for i in range(len(_conds) - 1, -1, -1):
            if _conds[i][3] in (self.pname, self.ename):
                self.cond_mark = min(self.cond_mark, i)
            else:
                break

    def mine(self, ship_name):
        return ship_name in (self.pname, self.ename)

    def nodes(self):
        return [n for n in _nodes[self.node_mark:] if n[1] in (self.pname, self.ename)]

    def conds(self):
        return [c for c in _conds[self.cond_mark:] if c[3] in (self.pname, self.ename)]

    def meta(self):
        winner = None
        if self.winner_role == "player":
            winner = self.pclass
        elif self.winner_role == "enemy":
            winner = self.eclass
        return dict(enemy=self.eclass, player=self.pclass, level=self.level, angle=None, human=1,
                    winner=winner, winner_role=self.winner_role,
                    t_end=round((self.end_t if self.end_t is not None else _now()) - self.t0, 2),
                    t0_game=round(self.t0, 3), finished=self.end_t is not None,
                    attacker_name=self.ename, target_name=self.pname,
                    tube_names={"A": _tubes(self.enemy), "T": _tubes(self.player)},
                    tree_nodes=[(sh, kind, nm) for (o, sh, kind, nm) in self.nodes()],
                    cond_nodes=[(owner, cname, carg) for (o, cname, carg, owner) in self.conds()],
                    sample_dt=SAMPLE_DT)

    def lists(self):
        return dict(choices=self.choices, hits=self.hits, track=self.track, allhits=self.allhits,
                    hitdetail=self.hitdetail, events=self.events, _smart=self.fs_counts["_smart"])

    def flush(self):
        """Append the rows added since the last flush (plus the current metadata and fire-script counters) to
        the journal: one JSON line, about 10 s of rows, so the cost stays flat as the fight grows."""
        os.makedirs(OUT_DIR, exist_ok=True)
        chunk = dict(meta=self.meta(), fs_counts={k: v for k, v in self.fs_counts.items() if k != "_smart"})
        for k, rows in self.lists().items():
            n = self.marks.get(k, 0)
            chunk[k] = rows[n:]
            self.marks[k] = len(rows)
        with open(self.part, "a") as f:
            f.write(json.dumps(chunk) + "\n")

    def write(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        doc = self.meta()
        doc.update(choices=self.choices, hits=self.hits, track=self.track, allhits=self.allhits,
                   hitdetail=self.hitdetail, events=self.events, fs_counts=self.fs_counts)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, self.path)
        try:
            os.remove(self.part)
        except OSError:
            pass


def read_partial(part_path):
    """The fight document rebuilt from a journal (<name>.part.jsonl) left by a crash or a killed game: the
    same fields as the finished file, with the last flush's metadata (finished=False unless it ended)."""
    doc = {}
    lists = dict(choices=[], hits=[], track=[], allhits=[], hitdetail=[], events=[], _smart=[])
    with open(part_path) as f:
        for line in f:
            try:
                chunk = json.loads(line)
            except ValueError:
                break                  # a line cut short by the crash
            doc.update(chunk["meta"])
            doc["fs_counts"] = chunk["fs_counts"]
            for k in lists:
                lists[k].extend(chunk.get(k, []))
    doc.setdefault("fs_counts", {})["_smart"] = lists.pop("_smart")
    doc.update(lists)
    return doc


def _tubes(ship):
    try:
        ts = ship.GetTorpedoSystem()
        return [ts.GetChildSubsystem(i).GetName() for i in range(ts.GetNumChildSubsystems())]
    except Exception:
        return []


def _rel(t):
    return round(t - _fight.t0, 3) if _fight is not None else None


# ── hooks ─────────────────────────────────────────────────────────────────────

def _patch(owner, attr, new):
    """setattr, remembering whether `attr` was the owner's own (restored) or inherited (deleted again)."""
    own = attr in vars(owner)
    _patched.append((owner, attr, vars(owner)[attr] if own else None, own))
    setattr(owner, attr, new)


def uninstall():
    """Restore every wrapped attribute and drop all state (tests; the live game never calls it)."""
    global _installed, _fight
    for owner, attr, orig, own in reversed(_patched):
        if own:
            setattr(owner, attr, orig)
        else:
            delattr(owner, attr)
    del _patched[:]
    del _nodes[:]
    del _conds[:]
    _beh.clear()
    _speed_last.clear()
    _fight = None
    _installed = False
    try:
        atexit.unregister(_finish)
    except Exception:
        pass


def install():
    """Wrap the factories and methods the duel harness wraps. Idempotent; pass-through when no fight is on."""
    global _installed
    if _installed:
        return
    _installed = True
    import App

    for fn in TREE_FACTORIES:
        f0 = getattr(App, fn, None)
        if f0 is None:
            continue

        def mk(f0=f0, kind=fn[:-7]):
            def w(*args, **kw):
                o = f0(*args, **kw)
                try:
                    _nodes.append((o, args[0].GetName(), kind, str(args[1])))
                except Exception:
                    pass
                return o
            return w
        _patch(App, fn, mk())
    c0 = getattr(App, "ConditionScript_Create", None)
    if c0 is not None:
        def cw(*args, **kw):
            o = c0(*args, **kw)
            try:
                _conds.append((o, str(args[1]), str(args[2])[:40] if len(args) > 2 else "",
                               _nodes[-1][1] if _nodes else "?"))
            except Exception:
                pass
            return o
        _patch(App, "ConditionScript_Create", cw)

    from engine.appc import combat
    orig_hit = combat.apply_hit

    def hit(ship, damage, hit_point, source, **kw):
        f = _fight
        if f is None or kw.get("_overflow_depth", 0) != 0 or not f.mine(_name(ship)):
            return orig_hit(ship, damage, hit_point, source, **kw)
        t = _rel(_now())
        try:
            sh = ship.GetShields() if hasattr(ship, "GetShields") else None
            before = sum(sh.GetCurrentShields(i) for i in range(6)) if sh is not None else 0.0
            hb = ship.GetHull().GetCondition() if ship.GetHull() is not None else 0.0
            f.allhits.append([t, _name(source) if source is not None else None, _name(ship),
                              kw.get("weapon_type"), round(float(damage), 2), round(before, 1), round(hb, 1)])
            if kw.get("weapon_type") == "phaser" and source is not None:
                sn = _name(source)
                aim = [v for (s_, ws), v in f.current.items() if s_ == sn and "Phasers" in ws]
                f.hits.append(dict(t=t, shooter=sn, target=_name(ship), aim=aim[0] if aim else None,
                                   dmg=round(float(damage), 2)))
            subs0 = {s.GetName(): s.GetCondition() for s in _all_subsystems(ship)}
            sh0 = [sh.GetCurrentShields(i) for i in range(6)] if sh is not None else None
        except Exception:
            subs0 = None
        r = orig_hit(ship, damage, hit_point, source, **kw)
        if subs0 is not None:
            try:
                sh1 = [sh.GetCurrentShields(i) for i in range(6)] if sh is not None else None
                lost = {}
                for s in _all_subsystems(ship):
                    d0 = subs0.get(s.GetName())
                    if d0 is not None and d0 - s.GetCondition() > 1e-6:
                        lost[s.GetName()] = round(d0 - s.GetCondition(), 2)
                f.hitdetail.append([t, _name(source) if source is not None else None, _name(ship),
                                    kw.get("weapon_type"), round(float(damage), 2),
                                    [round(x, 1) for x in sh0] if sh0 else None,
                                    [round(x, 1) for x in sh1] if sh1 else None, lost])
            except Exception:
                pass
        return r
    _patch(combat, "apply_hit", hit)

    from engine.appc.ships import ShipClass as _SC
    for nm, kind in (("SetImpulse", "I"), ("SetSpeed", "S")):
        orig = _SC.__dict__.get(nm)
        if orig is None:
            continue

        def sw(orig=orig, kind=kind, nm=nm):
            def w(self, *a, **k):
                _speed_depth[0] += 1
                try:
                    if a and _speed_depth[0] == 1 and _fight is not None:
                        n = _name(self)
                        if _fight.mine(n):
                            e = _speed_last.get(n)
                            if e is None:
                                _speed_last[n] = [kind, float(a[0]), _cur_script[0], 1]
                            else:
                                e[0], e[1], e[2], e[3] = kind, float(a[0]), _cur_script[0], e[3] + 1
                            if n == _fight.pname:
                                _fight.events.append([_rel(_now()), nm, round(float(a[0]), 4), _cur_script[0]])
                except Exception:
                    pass
                try:
                    return orig(self, *a, **k)
                finally:
                    _speed_depth[0] -= 1
            return w
        _patch(_SC, nm, sw())
    for nm in PLAYER_EVENTS:
        orig = getattr(_SC, nm, None)
        if orig is None:
            continue

        def ew(orig=orig, nm=nm):
            def w(self, *a, **k):
                try:
                    if _fight is not None and _name(self) == _fight.pname:
                        arg = a[0] if a else None
                        _fight.events.append([_rel(_now()), nm, _name(arg) if hasattr(arg, "GetName") else
                                              (arg if isinstance(arg, (int, float, str)) or arg is None else str(arg)),
                                              _cur_script[0]])
                except Exception:
                    pass
                return orig(self, *a, **k)
            return w
        _patch(_SC, nm, ew())

    for m in PLAIN_MODULES:
        try:
            cls = getattr(importlib.import_module("AI.PlainAI." + m), m)
        except Exception:
            continue
        up = cls.__dict__.get("Update")
        if up is None:
            continue

        def wrap(orig, m=m):
            def upd(self, *a, **k):
                try:
                    if _fight is not None:
                        key = (self.pCodeAI.GetShip().GetName(), m)
                        _beh[key] = _beh.get(key, 0) + 1
                except Exception:
                    pass
                prev = _cur_script[0]
                _cur_script[0] = m
                try:
                    return orig(self, *a, **k)
                finally:
                    _cur_script[0] = prev
            return upd
        _patch(cls, "Update", wrap(up))

    Pre = importlib.import_module("AI.Preprocessors")
    orig_choose = Pre.FireScript.ChooseTargetSubsystem

    def choose(self, pTarget):
        p = orig_choose(self, pTarget)
        try:
            f = _fight
            if f is not None:
                ship = self.pCodeAI.GetShip().GetName()
                if f.mine(ship):
                    ws = "+".join(w.GetName().replace(" ", "_") for w in getattr(self, "lWeapons", []))
                    nm = p.GetName().replace(" ", "_") if p else "-"
                    if f.current.get((ship, ws)) != nm:
                        f.choices.append(dict(t=_rel(_now()), ship=ship, ws=ws, sub=nm))
                    f.current[(ship, ws)] = nm
        except Exception:
            pass
        return p
    _patch(Pre.FireScript, "ChooseTargetSubsystem", choose)

    _install_command_hooks()
    atexit.register(_finish)

    # FireScript activity per game second (the twin of CombatTap's FSA rows): [FireSystemAtTarget calls,
    # CheckGoodShot passes], and every SmartPhaserShot decision. Recorded only when a compiled FireScript
    # exposing those hooks is present.
    try:
        from engine.appc.optimized_fire_script import CompiledFireScriptMixin as _CFS
    except ImportError:
        return
    if not all(hasattr(_CFS, n) for n in ("FireSystemAtTarget", "_smart_phaser_shot")):
        return

    def _fs_key(inst, w):
        try:
            node = inst.pCodeAI
            sn = node.GetShip().GetName()
            if _fight is None or not _fight.mine(sn):
                return None
            return "%d|%s|%s|%s" % (int(_now() - _fight.t0), sn, node.GetName(), w.GetName().replace(" ", "_"))
        except Exception:
            return None
    o_fire = _CFS.FireSystemAtTarget

    def fire(self, pWeaponSystem, pTarget, pSubsystem):
        k = _fs_key(self, pWeaponSystem)
        if k:
            _fight.fs_counts.setdefault(k, [0, 0])[0] += 1
        return o_fire(self, pWeaponSystem, pTarget, pSubsystem)
    _patch(_CFS, "FireSystemAtTarget", fire)
    o_good = Pre.FireScript.CheckGoodShot

    def good(self, pWeaponSystem, pTarget, pSubsystem):
        r = o_good(self, pWeaponSystem, pTarget, pSubsystem)
        if r:
            k = _fs_key(self, pWeaponSystem)
            if k:
                _fight.fs_counts.setdefault(k, [0, 0])[1] += 1
        return r
    _patch(Pre.FireScript, "CheckGoodShot", good)
    o_smart = _CFS._smart_phaser_shot

    def smart(self, pPhaserSystem, pTarget, pSubsystem):
        ok = o_smart(self, pPhaserSystem, pTarget, pSubsystem)
        f = _fight
        if f is None:
            return ok
        try:
            sn = self.pCodeAI.GetShip().GetName()
            if f.mine(sn):
                from engine.appc.optimized_fire_script import _dist
                aim = (pSubsystem or pTarget).GetWorldLocation()
                bank = pPhaserSystem.GetChildSubsystem(0) if pPhaserSystem.GetNumChildSubsystems() > 0 else None
                R = float(bank.GetMaxDamageDistance()) if bank is not None else 1.0
                f.fs_counts["_smart"].append([_rel(_now()), sn, round(_dist(aim, pPhaserSystem.GetWorldLocation()), 1),
                                              R, round(self.__dict__.get("_ofs_acc", -1), 3), 1 if ok else 0])
        except Exception:
            pass
        return ok
    _patch(_CFS, "_smart_phaser_shot", smart)


def _tsub(ship):
    try:
        return _name(ship.GetTargetSubsystem())
    except Exception:
        return None


def _install_command_hooks():
    """The player's weapon, power and ammo commands, and every torpedo launch by either fight ship, as events --
    the Dauntless twin of the original's xu rows (Oracle/OracleHuman.py). Event shapes:
      [t, "StartFiring"/"StopFiring", weapon system, script, target, target subsystem]   (player, press/release only)
      [t, "TorpedoLaunch", tube, ship, target, target subsystem]                         (both ships)
      [t, "SetPowerPercentageWanted", subsystem, script, value]                          (player)
      [t, "SetAmmoType", ammo index, script]  /  [t, "SetPowerLevel", phaser level, script] (player)"""
    from engine.appc import weapon_subsystems as _ws
    from engine.appc.subsystems import PoweredSubsystem as _PS

    def _subclasses(c):
        out = [c]
        for sc in c.__subclasses__():
            out += _subclasses(sc)
        return out
    held = {}            # id(weapon system) -> last logged firing state
    depth = [0]
    for cls in _subclasses(_ws.WeaponSystem):
        for nm in ("StartFiring", "StopFiring"):
            if nm not in vars(cls):
                continue

            def fw(orig=vars(cls)[nm], nm=nm):
                def w(self, *a, **k):
                    depth[0] += 1
                    try:
                        f = _fight
                        if depth[0] == 1 and f is not None and self.GetParentShip() is f.player:
                            on = nm == "StartFiring"
                            if held.get(id(self)) != on:
                                held[id(self)] = on
                                tgt = a[0] if (on and a) else None
                                f.events.append([_rel(_now()), nm, _name(self), _cur_script[0],
                                                 _name(tgt) if tgt is not None else None, _tsub(f.player)])
                    except Exception:
                        pass
                    try:
                        return orig(self, *a, **k)
                    finally:
                        depth[0] -= 1
                return w
            _patch(cls, nm, fw())

    o_tube = vars(_ws.TorpedoTube)["Fire"]

    def tube_fire(self, target=None, offset=None):
        r = o_tube(self, target, offset)
        try:
            f = _fight
            if r is True and f is not None:
                ship = self._climb_to_ship()
                if ship is f.player or ship is f.enemy:
                    f.events.append([_rel(_now()), "TorpedoLaunch", _name(self), _name(ship),
                                     _name(target) if target is not None else None, _tsub(ship)])
        except Exception:
            pass
        return r
    _patch(_ws.TorpedoTube, "Fire", tube_fire)

    last = {}            # (command, id(subsystem)) -> last logged value: a UI re-sending the same value is not news

    def player_cmd(cls, nm, label=None):
        orig = vars(cls)[nm]

        def w(self, *a, **k):
            try:
                f = _fight
                if f is not None and a:
                    ship = self.GetParentShip() if hasattr(self, "GetParentShip") else None
                    if ship is f.player and last.get((nm, id(self))) != a[0]:
                        last[(nm, id(self))] = a[0]
                        ev = [_rel(_now()), nm, _name(self) if label is None else a[0], _cur_script[0]]
                        if label is None:
                            ev.append(a[0])
                        f.events.append(ev)
            except Exception:
                pass
            return orig(self, *a, **k)
        _patch(cls, nm, w)
    player_cmd(_PS, "SetPowerPercentageWanted")
    player_cmd(_ws.TorpedoSystem, "SetAmmoType", label="value")
    player_cmd(_ws.PhaserSystem, "SetPowerLevel", label="value")


def _all_subsystems(ship):
    out = [ship.GetHull()] if ship.GetHull() is not None else []
    for sub in ship.GetSubsystems():
        if sub is not None and sub is not ship.GetHull():
            out.append(sub)
            try:
                out.extend(sub.GetChildSubsystem(i) for i in range(sub.GetNumChildSubsystems()))
            except Exception:
                pass
    return out


# ── rows ──────────────────────────────────────────────────────────────────────

def _pose(ship):
    p, R = ship.GetWorldLocation(), ship.GetWorldRotation()
    f, u = R.GetCol(1), R.GetCol(2)
    return [round(p.x, 3), round(p.y, 3), round(p.z, 3), round(f.x, 4), round(f.y, 4), round(f.z, 4),
            round(u.x, 4), round(u.y, 4), round(u.z, 4)]


def _status(ship):
    """The duel harness's status() fields, plus tg/ts/al/vel for human play."""
    out = {}
    try:
        out["imp"] = round(float(ship.GetImpulse()), 3)
    except Exception:
        pass
    try:
        out["pf"] = int(bool(ship.GetPhaserSystem().IsFiring()))
    except Exception:
        pass
    try:
        ts = ship.GetTorpedoSystem()
        out["am"] = ts.GetAmmoTypeNumber()
        out["ta"] = [ts.GetNumAvailableTorpsToType(i) for i in range(ts.GetNumAmmoTypes())]
        out["tr"] = [ts.GetChildSubsystem(i).GetNumReady() for i in range(ts.GetNumChildSubsystems())]
        out["td"] = [int(bool(ts.IsDisabled()))] + [int(bool(ts.GetChildSubsystem(i).IsDisabled()))
                                                    for i in range(ts.GetNumChildSubsystems())]
    except Exception:
        pass
    try:
        pw = ship.GetPowerSubsystem()
        if pw is not None:
            out["pw"] = [round(float(pw.GetMainBatteryPower()), 1), round(float(pw.GetBackupBatteryPower()), 1),
                         round(float(pw.GetMainBatteryLimit()), 1), round(float(pw.GetBackupBatteryLimit()), 1),
                         round(float(pw.GetPowerOutput()), 2), round(float(pw.GetAvailablePower()), 2),
                         round(float(pw.GetPowerDispensed()), 2)]
        pq = {}
        from engine.appc.subsystems import PoweredSubsystem
        for sub in ship.GetSubsystems():
            # only real power consumers: engine objects answer hasattr() for any name with a stub, so the hull and
            # the reactor used to log placeholder values here (Windows stub telemetry: 4720 hits per run)
            if not isinstance(sub, PoweredSubsystem):
                continue
            pq[sub.GetName().replace(" ", "_")] = [round(float(sub.GetPowerPercentageWanted()), 3),
                                                    round(float(sub.GetPowerPercentage()), 3),
                                                    round(float(sub.GetPowerReceived()), 3), int(bool(sub.IsOn()))]
        out["pq"] = pq
    except Exception:
        pass
    try:
        ck = ship.GetCloakingSubsystem()
        if ck is not None:
            out["ck"] = [int(bool(ck.IsCloaked())), int(bool(ck.IsCloaking())), int(bool(ck.IsDecloaking()))]
    except Exception:
        pass
    try:
        ew = {}
        for sysname in ("GetPhaserSystem", "GetPulseWeaponSystem"):
            ws = getattr(ship, sysname)()
            if ws is None:
                continue
            for i in range(ws.GetNumChildSubsystems()):
                o = ws.GetChildSubsystem(i)
                ew[o.GetName().replace(" ", "_")] = [round(float(o.GetChargeLevel()), 3), int(bool(o.IsFiring()))]
        out["ew"] = ew
    except Exception:
        pass
    try:
        sh = ship.GetShields()
        out["sh"] = round(sum(sh.GetCurrentShields(i) for i in range(6)), 1)
        out["hu"] = round(float(ship.GetHull().GetCondition()), 1)
    except Exception:
        pass
    try:
        tgt = ship.GetTarget()
        out["tg"] = _name(tgt) if tgt is not None else None
        sub = ship.GetTargetSubsystem() if hasattr(ship, "GetTargetSubsystem") else None
        out["ts"] = _name(sub) if sub is not None else None
    except Exception:
        pass
    try:
        out["al"] = int(ship.GetAlertLevel())
    except Exception:
        pass
    try:
        v = ship.GetVelocityTG()
        out["vel"] = round(math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z), 3)
    except Exception:
        pass
    return out


def _sample(f, now):
    fa = []
    for (o, sh, kind, nm) in f.nodes():
        try:
            fa.append(("1" if o.HasFocus() else "0") + ("1" if o.IsActive() else "0"))
        except Exception:
            fa.append("xx")
    cs = []
    for (o, cname, carg, owner) in f.conds():
        try:
            cs.append(str(int(o.GetStatus()))[:1])
        except Exception:
            cs.append("x")
    tp = []
    try:
        from engine.appc import projectiles as _proj
        for _p in list(_proj._active):
            _w = _p.GetWorldLocation()
            tp.append([int(_p.GetObjID()), round(float(_p.GetDamage()), 2),
                       round(_w.x, 2), round(_w.y, 2), round(_w.z, 2)])
    except Exception:
        tp = None
    spd = {("A" if n == f.ename else "T"): [e[0], round(e[1], 4), e[2], e[3]] for n, e in _speed_last.items()
           if n in (f.ename, f.pname)}
    _speed_last.clear()
    f.track.append(dict(t=round(now - f.t0, 2), A=_pose(f.enemy), T=_pose(f.player), sA=_status(f.enemy),
                        sT=_status(f.player), tp=tp, spd=spd,
                        beh={"%s|%s" % k: v for k, v in _beh.items() if v and k[0] in (f.ename, f.pname)},
                        tree="".join(fa), cond="".join(cs)))
    _beh.clear()


def _dying(ship):
    try:
        return bool(ship.IsDying()) or bool(ship.IsDead())
    except Exception:
        return True


def _other_ships(player):
    import App
    pset = player.GetContainingSet()
    if pset is None:
        return []
    out = []
    for o in pset.GetClassObjectList(App.CT_SHIP):
        s = App.ShipClass_Cast(o)
        if s is not None and s is not player and not _dying(s):
            out.append(s)
    return out


def _dist2(a, b):
    p, q = a.GetWorldLocation(), b.GetWorldLocation()
    return (p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2


def _finish():
    global _fight
    f = _fight
    if f is None:
        return
    try:
        f.write()
    except Exception:
        pass
    _fight = None


def tick(player):
    """Once per frame from host_loop. Starts, samples, flushes and closes the current fight."""
    global _fight
    if not ENABLED:
        return
    install()
    f = _fight
    if f is not None and player is not f.player:
        _finish()
        f = None
    if player is None:
        return
    now = _now()
    if f is None:
        others = _other_ships(player)
        if not others or _dying(player):
            return
        _fight = f = _Fight(player, min(others, key=lambda s: _dist2(s, player)))
    if now >= f.next_sample:
        _sample(f, now)
        # a fixed grid (t0 + k*SAMPLE_DT), not now + dt: frames are 1/60 s, so the latter drifts to ~0.117 s
        f.next_sample += SAMPLE_DT
        if f.next_sample <= now:
            f.next_sample = now + SAMPLE_DT
    if f.end_t is None and (_dying(f.player) or _dying(f.enemy)):
        f.end_t = now
        f.winner_role = "enemy" if _dying(f.player) else "player"
    if f.end_t is not None and now >= f.end_t + TAIL_S:
        _finish()
        return
    if now >= f.next_flush:
        f.next_flush = now + FLUSH_S
        try:
            f.flush()
        except Exception:
            pass
