"""Hull and shield totals for the scale bars, read from LOADED properties.

Runs BC's own loader sequence (loadspacehelper.py:88-91) into a scratch set:
ClearLocalTemplates -> reload the hardpoint module (our override pass fires in
the SDK loader here) -> LoadPropertySet. So the values are mod- and
override-aware, never parsed from hardpoint text. The primary hull is the FIRST
HullProperty (engine/appc/ships.py:1238). The manager's local templates AND
the per-leaf articulated-part snapshot (engine/appc/articulated_part.py's
_BY_LEAF, re-populated as a side effect of the same reload via
sdk_overrides.on_sdk_module_exec) are both snapshotted and restored in a
finally: a live mission -- and a live Ship Property Viewer session holding
unsaved rig edits -- must never notice. Spec §5.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShipStats:
    hull: float
    shields: float


def probe(ship_file) -> Optional[ShipStats]:
    st, _hp = _probe_with_hardpoint(ship_file)
    return st


def _probe_with_hardpoint(ship_file):
    """probe(), also returning the ship's HardpointFile (the persistent cache
    keys on that file's content). (None, None) when the probe fails."""
    import App
    from engine.appc.properties import HullProperty, ShieldProperty, TGModelPropertySet
    from engine.appc import articulated_part
    mgr = App.g_kModelPropertyManager
    snapshot = dict(mgr._local)
    # The reload below re-enters sdk_overrides.on_sdk_module_exec, which also
    # calls articulated_part.snapshot_for_leaf(leaf) -- overwriting the
    # process-wide _BY_LEAF[leaf] entry with freshly reloaded parts. The SPV
    # mutates those objects in place while it holds unsaved rig edits, so
    # this second registry needs the same snapshot/restore as _local.
    parts_snapshot = dict(articulated_part._BY_LEAF)
    try:
        ship_mod = importlib.import_module("ships." + ship_file)
        hp_file = ship_mod.GetShipStats()["HardpointFile"]
        mgr.ClearLocalTemplates()
        hp_mod = importlib.reload(importlib.import_module("ships.Hardpoints." + hp_file))
        pset = TGModelPropertySet()
        hp_mod.LoadPropertySet(pset)
        props = [p for _n, p in pset._entries]
        hull = next((p for p in props if isinstance(p, HullProperty)), None)
        shield = next((p for p in props if isinstance(p, ShieldProperty)), None)
        if hull is None:
            return None, hp_file
        total = sum(float(shield.GetMaxShields(f) or 0.0) for f in range(6)) if shield else 0.0
        return ShipStats(hull=float(hull.GetMaxCondition() or 0.0), shields=total), hp_file
    except Exception as e:                       # one bad ship costs only itself
        _log.info("quickbattle stats probe failed for %s: %s", ship_file, e)
        return None, None
    finally:
        mgr._local.clear()
        mgr._local.update(snapshot)
        articulated_part._BY_LEAF.clear()
        articulated_part._BY_LEAF.update(parts_snapshot)


# ── Persistent cache ───────────────────────────────────────────────────────
# A probe re-executes the ship's hardpoint script (0.07-0.15 s each, ~2.4 s for
# the 33 stock ships, all on the main thread): opening Quick Battle Setup the
# first time froze the game (user report 2026-10-16). The numbers only change
# when one of the files that produce them changes, so they persist on disk
# keyed by a fingerprint of exactly those files' CONTENT: the ship script and
# its hardpoint script as the import system resolves them now (mod overlay
# included), the two engine override passes, and STATS_CACHE_VERSION. Any
# change re-probes that ship; nothing stale can be served.
STATS_CACHE_VERSION = 1
_OVERRIDE_SOURCES = ("hardpoint_overrides.py", "ship_overrides.py")


def _cache_path():
    from engine.paths import PROJECT_ROOT
    return PROJECT_ROOT / "cache" / "quickbattle_stats.json"


def _source_bytes(modname: str) -> bytes:
    import importlib.util
    try:
        spec = importlib.util.find_spec(modname)
        origin = getattr(spec, "origin", None) if spec is not None else None
        if origin:
            with open(origin, "rb") as f:
                return origin.encode() + b"\0" + f.read()
    except Exception:  # noqa: BLE001 - an unresolvable module just never matches
        pass
    return b"?" + modname.encode()


def _overrides_digest() -> bytes:
    import hashlib
    import os
    h = hashlib.sha1()
    here = os.path.join(os.path.dirname(os.path.dirname(__file__)), "appc")
    for name in _OVERRIDE_SOURCES:
        try:
            with open(os.path.join(here, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"missing:" + name.encode())
    return h.digest()


def _fingerprint(ship_file: str, hp_file: str, overrides: bytes) -> str:
    import hashlib
    h = hashlib.sha1(b"v%d" % STATS_CACHE_VERSION)
    h.update(overrides)
    h.update(_source_bytes("ships." + ship_file))
    h.update(_source_bytes("ships.Hardpoints." + hp_file))
    return h.hexdigest()


class StatsCache:
    def __init__(self, persist: Optional[bool] = None):
        self._memo: dict = {}
        if persist is None:
            # Persist in the game; never from a test run (tests must not write
            # into the project's cache/ or read a cache another run left).
            import os
            persist = "PYTEST_CURRENT_TEST" not in os.environ
        self._persist = persist
        self._disk = None                 # {ship_file: {hp, fp, hull, shields}}
        self._overrides = None

    def reset(self) -> None:
        self._memo.clear()
        self._disk = None
        self._overrides = None

    def _load_disk(self) -> dict:
        if self._disk is None:
            self._disk = {}
            if self._persist:
                import json
                try:
                    data = json.loads(_cache_path().read_text())
                    if data.get("version") == STATS_CACHE_VERSION:
                        self._disk = dict(data.get("ships") or {})
                except Exception:  # noqa: BLE001 - missing / corrupt -> rebuild
                    self._disk = {}
        return self._disk

    def _save_disk(self) -> None:
        if not self._persist:
            return
        import json
        import os
        try:
            path = _cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": STATS_CACHE_VERSION, "ships": self._disk}, f, indent=0, sort_keys=True)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 - the cache is an optimisation
            _log.info("quickbattle stats cache not saved: %s", e)

    def get(self, ship_file):
        if ship_file in self._memo:
            return self._memo[ship_file]
        disk = self._load_disk()
        if self._overrides is None:
            self._overrides = _overrides_digest() if self._persist else b""
        rec = disk.get(ship_file)
        if rec is not None and rec.get("hp"):
            if rec.get("fp") == _fingerprint(ship_file, rec["hp"], self._overrides):
                st = (ShipStats(hull=float(rec["hull"]), shields=float(rec["shields"]))
                      if rec.get("hull") is not None else None)
                self._memo[ship_file] = st
                return st
        st, hp_file = _probe_with_hardpoint(ship_file)
        self._memo[ship_file] = st
        if hp_file:
            disk[ship_file] = {"hp": hp_file, "fp": _fingerprint(ship_file, hp_file, self._overrides),
                               "hull": None if st is None else st.hull,
                               "shields": None if st is None else st.shields}
            self._save_disk()
        return st

    def prewarm(self, ship_files) -> None:
        """Fill the memo (and the disk cache) for these ships now -- called at
        boot, so the screen's first open costs nothing."""
        for sf in ship_files:
            self.get(sf)

    def maxima(self, entries):
        hull = shields = 0.0
        for ce in entries:
            if not ce.playable:
                continue
            st = self.get(ce.ship_id)
            if st is not None:
                hull, shields = max(hull, st.hull), max(shields, st.shields)
        return hull, shields
