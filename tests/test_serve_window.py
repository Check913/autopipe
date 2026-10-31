# -*- coding: utf-8 -*-
"""Tests de run_window_once (serve-window) : une execution CI = une fenetre.

Contexte : le 2026-10-31 le cron 14:00Z n'a JAMAIS demarre (derive GitHub),
et `serve --once` publie immediatement -> video en retard. run_window_once
realise le cycle complet d'UNE fenetre : pre-generation anticipee, attente
exacte, preuve geo US EN DIRECT, upload prive puis bascule public a l'heure
pile. Ces tests verrouillent chaque sortie (succes, quota, horizon, doublon,
claim concurrent, geo bloquee, bascule ratee).
"""
import copy
import datetime as dt

import pytest

import autopipe.geo
import autopipe.scheduler as sched
from autopipe.config import get_paths
from autopipe.scheduler import _parse_hour, run_window_once
from autopipe.util import load_json, save_json

UTC = dt.timezone.utc
TODAY = "2026-10-31"
T14 = dt.datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)
T14_ISO = T14.isoformat()
T18_ISO = dt.datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC).isoformat()


def make_window_cfg(make_cfg, **over):
    """Cfg fenetres : 4 fenetres UTC + sched configuré comme en prod CI."""
    base = {
        "scheduler": {
            "state_file": "data/scheduler_state.json",
            "publish_lead_s": 120,
            "pregen_minutes": 20,
            "window_horizon_minutes": 75,
        },
        "upload": {
            "post_hours_utc": [14, 18, 22, 2],
            "daily_limit": 6,
        },
    }
    base.update(copy.deepcopy(over))
    return make_cfg(**base)


def write_pending(cfg, tmp_path, publish_at, **extra):
    """Ecrit data/pending_publish.json (contrat de publication)."""
    p = get_paths(cfg, root=tmp_path).data / "pending_publish.json"
    data = {"publish_at": publish_at, "video": "out/v.mp4", "title": "T"}
    data.update(extra)
    save_json(p, data)
    return p


def write_state(cfg, tmp_path, uploads=0, served=()):
    save_json(
        get_paths(cfg, root=tmp_path).root / "data/scheduler_state.json",
        {"day": TODAY, "uploads": uploads, "served_windows": list(served)})


def state_of(cfg, tmp_path):
    return load_json(
        get_paths(cfg, root=tmp_path).root / "data/scheduler_state.json", {})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fige l'horloge et neutralise pipeline/publish/geo/attente/alertes."""
    clock = {"now": dt.datetime(2026, 10, 31, 13, 40, 0, tzinfo=UTC)}
    calls = {"pipeline": [], "publish": [], "wait": [], "alert": [],
             "geo": []}

    def fake_pipeline(cfg, do_upload=False):
        calls["pipeline"].append({"do_upload": do_upload})
        res = {"video": "out/v.mp4", "idea": "idea-1", "title": "Titre test",
               "description": "Desc", "tags": ["tag"], "comment": "c",
               "pair_id": "pair_1", "quiz": {"q": "a"}, "idea_full": "full"}
        if do_upload:  # le pipeline reel upload -> il renvoie l'id YouTube
            res["youtube_id"] = "vid_fb"
        return res

    def fake_publish(cfg, paths, pending, pending_file=None):
        calls["publish"].append({"pending": dict(pending),
                                 "pending_file": pending_file})
        return {"published": True, "youtube_id": "vid_ok"}

    def fake_wait(target):
        calls["wait"].append(target)

    def fake_alert(cfg, subject, body):
        calls["alert"].append(subject)

    def fake_geo(cfg, force=False):
        calls["geo"].append({"force": force})
        return {"ok": True, "country": "US", "ip": "203.0.113.7",
                "egress": "test"}

    monkeypatch.setattr(sched, "_utcnow", lambda: clock["now"])
    monkeypatch.setattr(sched, "run_pipeline_once", fake_pipeline)
    monkeypatch.setattr(sched, "publish_pending_video", fake_publish)
    monkeypatch.setattr(sched, "_wait_until", fake_wait)
    monkeypatch.setattr(sched, "_alert", fake_alert)
    monkeypatch.setattr(autopipe.geo, "check_exit_country", fake_geo)
    # paths.root = cwd (get_paths(cfg)) -> isolate l'etat dans tmp_path.
    monkeypatch.chdir(tmp_path)
    return {"clock": clock, "calls": calls, "tmp": tmp_path}


# --- Sorties non bloquantes -------------------------------------------------

def test_already_served_skips_and_clears_pending(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    write_state(cfg, env["tmp"], uploads=1, served=[T14_ISO])
    pend = write_pending(cfg, env["tmp"], T14_ISO)
    res = run_window_once(cfg, at_hour=14)
    assert res == {"ok": True, "published": False, "reason": "already_served",
                   "target": T14_ISO}
    assert not pend.exists()
    assert env["calls"]["pipeline"] == []
    assert env["calls"]["publish"] == []


def test_outside_horizon_late_window_ignored(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 16, 20, tzinfo=UTC)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "outside_horizon"
    assert res["delta_min"] == 140.0
    assert env["calls"]["pipeline"] == []
    assert env["calls"]["publish"] == []


def test_outside_horizon_too_far_window_ignored(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    res = run_window_once(cfg, at_hour=14)
    assert res["reason"] == "outside_horizon"
    assert res["delta_min"] == -120.0
    assert env["calls"]["pipeline"] == []


def test_quota_blocks_window_without_contract(env, make_cfg):
    cfg = make_window_cfg(make_cfg, upload={"post_hours_utc": [14, 18, 22, 2],
                                            "daily_limit": 2})
    write_state(cfg, env["tmp"], uploads=2)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "quota"
    assert env["calls"]["pipeline"] == []
    assert env["calls"]["publish"] == []


def test_no_windows_when_post_hours_empty(env, make_cfg):
    cfg = make_window_cfg(make_cfg, upload={"post_hours_utc": [],
                                            "daily_limit": 6})
    res = run_window_once(cfg, at_hour=None)
    assert res == {"ok": False, "reason": "no_windows"}


def test_pending_mismatch_keeps_other_contract(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    pend = write_pending(cfg, env["tmp"], T18_ISO)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "pending_mismatch"
    assert res["pending_publish_at"] == T18_ISO
    assert load_json(pend)["publish_at"] == T18_ISO
    assert env["calls"]["publish"] == []


def test_claimed_by_other_exits_cleanly(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 13, 58, tzinfo=UTC)
    pend = write_pending(cfg, env["tmp"], T14_ISO,
                         claimed_at="2026-10-31T13:57:00+00:00")
    res = run_window_once(cfg, at_hour=14)
    assert res == {"ok": True, "published": False,
                   "reason": "claimed_by_other", "target": T14_ISO}
    assert load_json(pend)["publish_at"] == T14_ISO
    assert env["calls"]["publish"] == []
    assert env["calls"]["pipeline"] == []


# --- Chemin nominal ---------------------------------------------------------

def test_future_window_pregen_waits_geo_then_publishes(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    res = run_window_once(cfg, at_hour=14)  # now 13:40 -> cible 14:00
    assert res["ok"] is True
    assert res["published"] is True
    assert res["youtube_id"] == "vid_ok"
    assert res["late_s"] == 0
    # Pre-generation SANS upload, attente exacte jusqu'a target - lead.
    assert env["calls"]["pipeline"] == [{"do_upload": False}]
    assert env["calls"]["wait"] == [T14 - dt.timedelta(seconds=120)]
    assert env["calls"]["geo"] == [{"force": True}]  # preuve geo EN DIRECT
    assert len(env["calls"]["publish"]) == 1
    # Contrat consomme, fenetre marquee servie, quota +1, dernier run trace.
    assert not (env["tmp"] / "data" / "pending_publish.json").exists()
    st = state_of(cfg, env["tmp"])
    assert st["uploads"] == 1
    assert T14_ISO in st["served_windows"]
    last = load_json(env["tmp"] / "data" / "last_run.json")
    assert last["youtube_id"] == "vid_ok"


def test_pending_contract_reused_without_new_pregen(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    write_pending(cfg, env["tmp"], T14_ISO)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 13, 50, tzinfo=UTC)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is True and res["published"] is True
    assert env["calls"]["pipeline"] == []  # pas de 2e generation
    assert len(env["calls"]["publish"]) == 1


def test_fresh_claim_with_video_id_flips_without_reupload(env, make_cfg):
    # Job concurrent a uploade la video privee (claim frais + youtube_id) :
    # ce job ne fait que la bascule public -> jamais de doublon.
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 13, 58, tzinfo=UTC)
    write_pending(cfg, env["tmp"], T14_ISO, youtube_id="priv1",
                  claimed_at="2026-10-31T13:57:00+00:00")
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is True and res["youtube_id"] == "vid_ok"
    assert env["calls"]["pipeline"] == []
    assert env["calls"]["publish"][0]["pending"]["youtube_id"] == "priv1"


# --- Contrats perimes -------------------------------------------------------

def test_stale_pending_falls_back_to_immediate_publish(env, make_cfg):
    cfg = make_window_cfg(make_cfg)
    write_pending(cfg, env["tmp"], T14_ISO)  # perime (pas d'upload prive)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 14, 5, tzinfo=UTC)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is True and res["published"] is True
    assert env["calls"]["pipeline"] == [{"do_upload": True}]
    assert env["calls"]["publish"] == []  # l'ancien contrat n'est PAS publie
    assert not (env["tmp"] / "data" / "pending_publish.json").exists()


def test_stale_pending_with_private_video_is_rescued(env, make_cfg):
    # Une video PRIVEE deja uploadee n'est jamais abandonnee : bascule public
    # de sauvetage, puis fenetre servie par publication de secours.
    cfg = make_window_cfg(make_cfg)
    write_pending(cfg, env["tmp"], T14_ISO, youtube_id="priv1")
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 14, 5, tzinfo=UTC)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is True and res["published"] is True
    assert env["calls"]["publish"][0]["pending"]["youtube_id"] == "priv1"
    assert env["calls"]["pipeline"] == [{"do_upload": True}]


# --- Echecs maitrises -------------------------------------------------------

def test_geo_blocked_cancels_publish_but_keeps_contract(env, make_cfg,
                                                       monkeypatch):
    import autopipe.geo

    cfg = make_window_cfg(make_cfg)

    def bad_geo(cfg_, force=False):
        env["calls"]["geo"].append({"force": force})
        return {"ok": False, "country": "FR", "ip": "9.9.9.9",
                "egress": "test", "reason": "pays hors US"}

    monkeypatch.setattr(autopipe.geo, "check_exit_country", bad_geo)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "geo_blocked"
    assert res["geo"]["country"] == "FR"
    assert env["calls"]["publish"] == []  # JAMAIS d'upload sans US prouve
    assert len(env["calls"]["alert"]) == 1
    # Le contrat pre-genere survit pour le cron de secours.
    pend = load_json(env["tmp"] / "data" / "pending_publish.json")
    assert pend["publish_at"] == T14_ISO


def test_flip_failed_keeps_video_id_for_retry(env, make_cfg, monkeypatch):
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 13, 58, tzinfo=UTC)
    write_pending(cfg, env["tmp"], T14_ISO)

    def flip_fails(cfg_, paths, pending, pending_file=None):
        env["calls"]["publish"].append({"pending": dict(pending)})
        return {"published": False, "reason": "flip_failed",
                "youtube_id": "vid9"}

    monkeypatch.setattr(sched, "publish_pending_video", flip_fails)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "flip_failed"
    assert res["youtube_id"] == "vid9"
    # Contrat conserve AVEC l'id : le cron de secours rebasculera sans
    # re-uploader.
    pend = load_json(env["tmp"] / "data" / "pending_publish.json")
    assert pend["youtube_id"] == "vid9"
    assert state_of(cfg, env["tmp"]).get("uploads", 0) == 0


def test_publish_error_maps_to_reason(env, make_cfg, monkeypatch):
    cfg = make_window_cfg(make_cfg)
    env["clock"]["now"] = dt.datetime(2026, 10, 31, 13, 58, tzinfo=UTC)
    write_pending(cfg, env["tmp"], T14_ISO)

    def upload_fails(cfg_, paths, pending, pending_file=None):
        return {"published": False, "reason": "upload_failed"}

    monkeypatch.setattr(sched, "publish_pending_video", upload_fails)
    res = run_window_once(cfg, at_hour=14)
    assert res["ok"] is False
    assert res["reason"] == "upload_failed"
    assert state_of(cfg, env["tmp"]).get("uploads", 0) == 0


# --- _parse_hour ------------------------------------------------------------

def test_parse_hour_accepts_various_formats():
    assert _parse_hour("14") == 14
    assert _parse_hour("14:00") == 14
    assert _parse_hour(14) == 14
    assert _parse_hour(" 6:30 ") == 6
    assert _parse_hour("2") == 2


def test_parse_hour_rejects_invalid():
    with pytest.raises(ValueError):
        _parse_hour("")
    with pytest.raises(ValueError):
        _parse_hour(None)
    with pytest.raises(ValueError):
        _parse_hour("abc")
    with pytest.raises(ValueError):
        _parse_hour("99")
