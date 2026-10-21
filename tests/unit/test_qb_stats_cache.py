"""Quick Battle stats persist on disk, keyed by the producing files' content.

Opening Quick Battle Setup the first time froze the game for seconds: every
ship's stats re-executed its hardpoint script (user report 2026-10-16). The
stats now persist (cache/quickbattle_stats.json) under a content fingerprint,
and the panel prewarms them at boot.
"""
from engine.quickbattle import stats


def _fake_probe(calls):
    def probe(ship_file):
        calls.append(ship_file)
        return stats.ShipStats(hull=100.0, shields=200.0), "hp_" + ship_file
    return probe


def test_second_cache_reads_disk_without_probing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stats, "_probe_with_hardpoint", _fake_probe(calls))
    monkeypatch.setattr(stats, "_cache_path", lambda: tmp_path / "qb.json")
    monkeypatch.setattr(stats, "_source_bytes", lambda m: m.encode())
    a = stats.StatsCache(persist=True)
    assert a.get("Galaxy") == stats.ShipStats(100.0, 200.0)
    b = stats.StatsCache(persist=True)                 # a new launch
    assert b.get("Galaxy") == stats.ShipStats(100.0, 200.0)
    assert calls == ["Galaxy"]


def test_changed_source_reprobes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stats, "_probe_with_hardpoint", _fake_probe(calls))
    monkeypatch.setattr(stats, "_cache_path", lambda: tmp_path / "qb.json")
    src = {"v": b"one"}
    monkeypatch.setattr(stats, "_source_bytes", lambda m: m.encode() + src["v"])
    stats.StatsCache(persist=True).get("Galaxy")
    src["v"] = b"two"                                  # hardpoint edited
    stats.StatsCache(persist=True).get("Galaxy")
    assert calls == ["Galaxy", "Galaxy"]


def test_tests_never_persist_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(stats, "_probe_with_hardpoint", _fake_probe([]))
    path = tmp_path / "qb.json"
    monkeypatch.setattr(stats, "_cache_path", lambda: path)
    stats.StatsCache().get("Galaxy")                   # PYTEST_CURRENT_TEST is set
    assert not path.exists()
