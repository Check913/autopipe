# -*- coding: utf-8 -*-
"""MODULE 5 : suivi YPP, ETA et revenus estimes (aucun reseau)."""
import json


# ------------------------------------------------------------- helpers purs
def test_parse_iso_variants():
    from autopipe.monetization import _parse_iso
    utc = _parse_iso("2026-10-29T00:42:00Z")
    assert utc is not None and utc.year == 2026
    naive = _parse_iso("2026-10-29 00:42:00")
    assert naive.tzinfo is None
    assert naive == utc.replace(tzinfo=None)
    assert _parse_iso("") is None
    assert _parse_iso(None) is None
    assert _parse_iso("garbage") is None


def test_pct_and_eta_math():
    from autopipe.monetization import _delta_per_day, _eta_days, _pct
    assert _pct(50, 100) == 50.0
    assert _pct(0, 100) == 0.0
    assert _pct(1, None) is None
    assert _eta_days(1000, 1000, None) == 0
    assert _eta_days(1500, 1000, 0) == 0          # deja atteint
    assert _eta_days(500, 1000, 10) == 50
    assert _eta_days(500, 1000, 0) is None        # velocite inconnue
    assert _eta_days(None, 1000, None) is None
    assert _delta_per_day(100, 300, 2.0) == 100.0
    assert _delta_per_day(300, 100, 2.0) == 0.0   # pas de velocite negative
    assert _delta_per_day(None, 300, 2.0) is None


# ------------------------------------------------------------------ run_check
def test_run_check_eligible(monkeypatch, make_cfg):
    from autopipe import monetization as mon
    cfg = make_cfg(monetization={
        "enabled": True, "check_hours": 12, "target_subs": 1000,
        "target_shorts_views_90d": 10_000_000,
        "target_watch_hours_12m": 4000, "rpm_shorts_usd": 0.08})
    monkeypatch.setattr(mon, "_channel_stats", lambda cfg: {
        "channel_id": "ch123", "title": "TestChannel", "subs": 1000,
        "hidden_subs": False, "total_views": 123,
        "video_count": 5, "channel_created_at": "2026-10-01T00:00:00Z"})
    monkeypatch.setattr(mon, "_views_90d",
                        lambda cfg: (10_000_000, 12))
    monkeypatch.setattr(mon, "_watch_hours_12m", lambda cfg: None)

    doc = mon.run_check(cfg, force=True)
    assert doc["errors"] == []
    assert doc["subs"] == 1000
    assert doc["shorts_views_90d"] == 10_000_000
    assert doc["n_shorts_90d"] == 12
    assert doc["watch_hours_12m"] is None
    assert doc["ypp"]["eligible"] is True
    assert doc["channel"]["title"] == "TestChannel"
    assert doc["est_monthly_revenue_usd"] == round(
        10_000_000 / 90.0 * 30.0 * 0.08 / 1000.0, 2)
    # Artefacts ecrits
    assert mon._progress_file(cfg).exists()
    hist = mon._history_file(cfg).read_text(encoding="utf-8").strip()
    snap = json.loads(hist.splitlines()[-1])
    assert snap["eligible"] is True
    # Cache frais -> report() relit sans re-call (pas d'exception)
    assert "ELIGIBLE" in mon.report(cfg)


def test_run_check_not_eligible_no_velocity(make_cfg, monkeypatch):
    from autopipe import monetization as mon
    cfg = make_cfg(monetization={
        "enabled": True, "check_hours": 12, "target_subs": 1000,
        "target_shorts_views_90d": 10_000_000,
        "target_watch_hours_12m": 4000, "rpm_shorts_usd": 0.08})
    monkeypatch.setattr(mon, "_channel_stats", lambda cfg: {
        "channel_id": "ch", "title": "T", "subs": 10, "hidden_subs": False,
        "total_views": 1, "video_count": 1,
        "channel_created_at": "2026-10-28T00:00:00Z"})
    monkeypatch.setattr(mon, "_views_90d", lambda cfg: (5000, 2))
    monkeypatch.setattr(mon, "_watch_hours_12m", lambda cfg: 12.5)
    doc = mon.run_check(cfg, force=True)
    assert doc["ypp"]["eligible"] is False
    # 1er snapshot : aucune velocite connue -> ETA inconnus, pas de crash
    assert doc["velocities_per_day"]["subs"] is None
    assert doc["eta_days"]["subs"] is None
    assert doc["eta_days"]["shorts_views_90d"] is None
    assert doc["watch_hours_12m"] == 12.5
    # Route heures seule complete (10M vues impossible) mais subs < 1000
    assert doc["ypp"]["checks"]["watch_hours"]["current"] == 12.5


def test_run_check_channel_failure_degrades(monkeypatch, make_cfg):
    """channels.list KO -> doc degrade, jamais d'exception."""
    from autopipe import monetization as mon
    cfg = make_cfg(monetization={"enabled": True})

    def boom(cfg):
        raise RuntimeError("network down")

    monkeypatch.setattr(mon, "_channel_stats", boom)
    monkeypatch.setattr(mon, "_views_90d", lambda cfg: (0, 0))
    monkeypatch.setattr(mon, "_watch_hours_12m", lambda cfg: None)
    doc = mon.run_check(cfg, force=True)
    assert doc["subs"] is None
    assert doc["ypp"]["eligible"] is False
    assert doc["errors"]


def test_run_check_ttl_cache(monkeypatch, make_cfg):
    from autopipe import monetization as mon
    cfg = make_cfg(monetization={"enabled": True, "check_hours": 12})
    calls = []

    def counter(cfg):
        calls.append(1)
        return {"channel_id": "c", "title": "T", "subs": 1,
                "hidden_subs": False, "total_views": 0, "video_count": 0,
                "channel_created_at": None}

    monkeypatch.setattr(mon, "_channel_stats", counter)
    monkeypatch.setattr(mon, "_views_90d", lambda cfg: (0, 0))
    monkeypatch.setattr(mon, "_watch_hours_12m", lambda cfg: None)
    mon.run_check(cfg, force=True)
    mon.run_check(cfg)  # cache frais
    assert len(calls) == 1


def test_report_empty_state(monkeypatch, make_cfg):
    from autopipe import monetization as mon
    cfg = make_cfg(monetization={"enabled": True})
    monkeypatch.setattr(mon, "_channel_stats",
                        lambda cfg: {"channel_id": "c", "title": "T",
                                     "subs": 2, "hidden_subs": False,
                                     "total_views": 0, "video_count": 0,
                                     "channel_created_at": None})
    monkeypatch.setattr(mon, "_views_90d", lambda cfg: (0, 0))
    monkeypatch.setattr(mon, "_watch_hours_12m", lambda cfg: None)
    out = mon.report(cfg)
    assert "ELIGIBLE" in out and "pas encore" in out
