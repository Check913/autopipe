# -*- coding: utf-8 -*-
"""Tests de run_window_once (serve-window) : une execution CI = une fenetre.

Contexte : le 2026-10-31 le cron 14:00Z n'a JAMAIS demarre (derive GitHub),
et `serve --once` publie immediatement -> video en retard. run_window_once
realise le cycle complet d'UNE fenetre : pre-generation anticipee, attente
exacte, preuve geo US EN DIRECT (3 essais live), puis upload PUBLIC DIRECT a
l'heure pile (jamais privee puis publique, aucune bascule). Ces tests
verrouillent chaque sortie (succes, quota, horizon, doublon, claim
concurrent, geo bloquee, bascule de sauvetage ratee) et le verrou "public
direct" de publish_pending_video / status_payload.
"""
import copy
import datetime as dt

import pytest

import autopipe.geo
import autopipe.scheduler as sched
from autopipe.config import get_paths
from autopipe.scheduler import _parse_hour, run_window_once
from autopipe.upload import status_payload
from autopipe.util import load_json, save_json
from pathlib import Path

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
    # _ensure_geo_us (3 essais, 20 s entre chaque) et _flip_public_now
    # (4 essais, 5 s) : aucun vrai sleep dans les tests.
    monkeypatch.setattr(sched.time, "sleep", lambda *_: None)
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
    # Preuve geo EN DIRECT (force=True, jamais le cache) : 1 appel car
    # le 1er essai reussit (les 3 essais ne servent qu'en cas d'echec).
    assert env["calls"]["geo"] == [{"force": True}]
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
    # 3 essais live avant abandon (fail-closed US ou rien).
    assert len(env["calls"]["geo"]) == 3
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


# --- Verrou "PUBLIC DIRECT" (publish_pending_video + status_payload) ---------

def test_publish_pending_video_uploads_public_directly(make_cfg, tmp_path,
                                                       monkeypatch):
    """Le bot uploade LUI-MEME en public : jamais prive, aucune bascule."""
    cfg = make_cfg(scheduler={"engage_first_hour": False})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "v.mp4").write_bytes(b"vid")
    pub_at = dt.datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
    pend_file = tmp_path / "data" / "pending_publish.json"
    pend_file.parent.mkdir()
    # Metadonnees realistes : elles doivent franchir le gate qualite
    # SANS aucune reparation (titre 15-50 chars sans emoji ni prefixe
    # compteur ni mot banni, description < 5000 octets, tags non vides
    # et sous budget).
    title = "3 Facts No One Tells You About Bees"
    description = "Bees never sleep and other facts #facts"
    tags = ["facts"]
    save_json(pend_file, {"publish_at": pub_at.isoformat(),
                          "video": "out/v.mp4", "title": title,
                          "description": description, "tags": tags})
    pending = load_json(pend_file)

    calls = {"upload": [], "flip": [], "wait": []}
    monkeypatch.setattr(sched, "_utcnow",
                        lambda: dt.datetime(2026, 10, 31, 13, 40, tzinfo=UTC))
    monkeypatch.setattr(sched, "_wait_until",
                        lambda target: calls["wait"].append(target))
    monkeypatch.setattr(sched, "_flip_public_now",
                        lambda cfg_, vid: calls["flip"].append(vid))

    def fake_upload(*args, **kwargs):
        calls["upload"].append({"args": args, "kwargs": kwargs})
        return "vid_new"

    monkeypatch.setattr(sched, "upload_video", fake_upload)
    monkeypatch.setattr(sched.analytics, "record", lambda *a, **k: None)
    import autopipe.registry as registry
    monkeypatch.setattr(registry, "register", lambda *a, **k: None)

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending, pending_file=pend_file)
    assert res == {"published": True, "youtube_id": "vid_new",
                   "video": str(Path("out/v.mp4"))}
    # Upload direct en public : AUCUN argument privacy, AUCUNE bascule.
    assert calls["upload"][0]["kwargs"] == {}
    assert calls["upload"][0]["args"] == (Path("out/v.mp4"), title,
                                          description, tags, cfg)
    assert calls["flip"] == []
    # Attente sans derive jusqu'a la cible PILE, puis upload public.
    assert calls["wait"] == [pub_at]
    # Point de reprise : l'id persiste TOUT DE SUITE apres l'upload.
    assert load_json(pend_file)["youtube_id"] == "vid_new"


def test_publish_pending_video_legacy_id_rescued_without_reupload(
        make_cfg, tmp_path, monkeypatch):
    """Contrat historique avec youtube_id : bascule public IMMEDIATE,
    JAMAIS de re-upload (et fichier video manquant non bloquant)."""
    cfg = make_cfg(scheduler={"engage_first_hour": False})
    monkeypatch.chdir(tmp_path)
    pending = {"publish_at": T14_ISO, "video": "out/v.mp4",
               "title": "T", "youtube_id": "priv1"}

    calls = {"upload": [], "flip": []}
    monkeypatch.setattr(sched, "_utcnow",
                        lambda: dt.datetime(2026, 10, 31, 14, 5, tzinfo=UTC))
    monkeypatch.setattr(
        sched, "_flip_public_now",
        lambda cfg_, vid: (calls["flip"].append(vid), True)[1])
    monkeypatch.setattr(
        sched, "upload_video",
        lambda *a, **k: calls["upload"].append((a, k)))
    monkeypatch.setattr(sched.analytics, "record", lambda *a, **k: None)
    import autopipe.registry as registry
    monkeypatch.setattr(registry, "register", lambda *a, **k: None)

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending)
    assert res == {"published": True, "youtube_id": "priv1",
                   "video": str(Path("out/v.mp4"))}
    assert calls["flip"] == ["priv1"]   # bascule IMMEDIATE
    assert calls["upload"] == []        # JAMAIS de re-upload


def test_publish_pending_video_flip_failure_alerts(make_cfg, tmp_path,
                                                   monkeypatch):
    """Bascule impossible -> flip_failed + alerte, et re-upload INTERDIT."""
    cfg = make_cfg(scheduler={"engage_first_hour": False})
    monkeypatch.chdir(tmp_path)
    pending = {"publish_at": T14_ISO, "video": "out/v.mp4",
               "title": "T", "youtube_id": "priv1"}
    alerts = []
    monkeypatch.setattr(sched, "_alert",
                        lambda cfg_, subj, body: alerts.append(subj))
    monkeypatch.setattr(sched, "_flip_public_now", lambda cfg_, vid: False)

    def forbidden_upload(*args, **kwargs):
        raise AssertionError("re-upload interdit quand l'id existe deja")

    monkeypatch.setattr(sched, "upload_video", forbidden_upload)
    monkeypatch.setattr(sched.analytics, "record", lambda *a, **k: None)

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending)
    assert res["published"] is False
    assert res["reason"] == "flip_failed"
    assert res["youtube_id"] == "priv1"
    assert len(alerts) == 1  # alerte : action manuelle requise


def test_status_payload_forces_public_by_default(make_cfg):
    # Garde-fou : meme une demande "private" est ecrasee en public.
    cfg = make_cfg(upload={"privacy": "private", "made_for_kids": False})
    assert status_payload(cfg)["privacyStatus"] == "public"


def test_status_payload_respects_explicit_opt_out(make_cfg):
    # Seul force_public = False explicite permet une publication non-publique.
    cfg = make_cfg(upload={"privacy": "private", "force_public": False})
    assert status_payload(cfg)["privacyStatus"] == "private"


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
