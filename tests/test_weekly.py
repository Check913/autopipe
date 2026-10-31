# -*- coding: utf-8 -*-
"""Tests du MODULE 7 (weekly.py) : join multi-sources, rapport hebdo,
directives, ban OPT-IN des familles de dead shorts.

Monde de test (`_world`) :
  - v1 : 1000 vues / 40 likes / 5 comm / 2 j   -> top 1, retention 78.5
  - v2 : 200 vues / 15 likes / 0 comm / 10 j   -> hors fenetre 7 j, CTA video
  - v3 : 0 vue / 70 j                          -> dead short, famille number
  - v4 : 150 vues / 3 likes / 5 comm / 3 j     -> top 2, pas de retention (n/d)
  - v5 : 0 vue, date invalide                  -> ni top ni dead (age None)
  - v6 (ajoute a la demande) : 0 vue / 80 j, famille statement
"""
import datetime as dt
import json

from autopipe import weekly as W
from autopipe.config import get_paths
from autopipe.util import save_json

REG = {
    "v1": {"title": "Bank crisis explained", "hook": "Why do banks fear this crisis?",
           "duration_format": "0:35", "source": "ideas"},
    "v2": {"title": "Simple money truth", "hook": "The simple truth about money",
           "duration_format": "0:58", "source": "remake"},
    "v3": {"title": "5 savings rules", "hook": "5 savings rules nobody told you",
           "duration_format": "0:45", "source": "ideas"},
    "v4": {"title": "Save at 20", "hook": "What happens if you save at 20?",
           "duration_format": "0:40", "source": "ideas"},
}


def _iso(days_ago):
    d = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _world(cfg):
    """Construit toutes les sources de donnees du rapport hebdo."""
    paths = get_paths(cfg)
    save_json(paths.analytics / "latest.json", [
        {"id": "v1", "views": 1000, "likes": 40, "comments": 5,
         "published": _iso(2), "title": "Bank crisis explained"},
        {"id": "v2", "views": 200, "likes": 15, "comments": 0,
         "published": _iso(10), "title": "Simple money truth"},
        {"id": "v3", "views": 0, "likes": 0, "comments": 0,
         "published": _iso(70), "title": "5 savings rules"},
        {"id": "v4", "views": 150, "likes": 3, "comments": 5,
         "published": _iso(3), "title": "Save at 20"},
        {"id": "v5", "views": 0, "likes": 0, "comments": 0,
         "published": "not-a-date", "title": "Backup upload test"},
    ])
    save_json(paths.analytics / "retention_latest.json", {"rows": [
        {"id": "v1", "engaged_ratio": 78.5, "avg_view_pct": 70.0},
        {"id": "v2", "avg_view_pct": 41.2},
    ]})
    save_json(paths.registry / "published.json",
              {"videos": REG, "updated": _iso(0)})
    save_json(paths.optimizer / "patterns.json",
              {"mode": "v3", "lessons": 12, "patterns": ["slow_zoom"],
               "baseline_delta": {"retention": 1.2}})
    save_json(paths.optimizer / "killswitch.json", {
        "banned_families": {
            "negation": {"remaining": 5, "banned_at": "2026-10-01T00:00:00+00:00",
                         "last_ids": ["v0"]}},
        "niche": {"alert": True, "topic": "quiz", "escape_remaining": 4},
    })
    rej = paths.compliance / "rejected.jsonl"
    rej.write_text(
        json.dumps({"idea_id": "i1",
                    "reasons": ["originality: jaccard=0.71 vs a1b2"]}) + "\n" +
        json.dumps({"idea_id": "i2", "reasons": ["fatigue: run=5/5"]}) + "\n",
        encoding="utf-8")
    return paths


def _add_video(cfg, vid, views, likes, comments, days_ago, hook, title):
    """Ajoute une video au snapshot analytics + registre."""
    paths = get_paths(cfg)
    latest = json.loads(
        (paths.analytics / "latest.json").read_text(encoding="utf-8"))
    latest.append({"id": vid, "views": views, "likes": likes,
                   "comments": comments, "published": _iso(days_ago),
                   "title": title})
    save_json(paths.analytics / "latest.json", latest)
    reg = json.loads(
        (paths.registry / "published.json").read_text(encoding="utf-8"))
    reg["videos"][vid] = {"hook": hook, "title": title}
    save_json(paths.registry / "published.json", reg)


# --------------------------------------------------------------------------
# Join principal + totaux
# --------------------------------------------------------------------------
def test_totals_and_join(make_cfg):
    cfg = make_cfg()
    _world(cfg)
    rep = W.weekly_report(cfg)
    assert rep["totals"] == {"videos": 5, "published_in_window": 2,
                             "views": 1350, "views_in_window": 1150,
                             "likes": 58, "comments": 10}
    assert [r["video_id"] for r in rep["top_performers"]] == ["v1", "v4"]
    assert [r["video_id"] for r in rep["dead_shorts"]] == ["v3"]
    assert rep["dead_shorts"][0]["hook_family"] == "number"
    assert rep["dead_shorts"][0]["age_days"] >= 60
    top = {r["video_id"]: r for r in rep["top_performers"]}
    assert top["v1"]["engagement_pct"] == 78.5
    assert top["v4"]["engagement_pct"] is None  # n/d, jamais un faux 0%
    assert rep["patterns"]["mode"] == "v3"
    assert rep["patterns"]["lessons"] == 12
    ks = rep["kill_switch"]
    assert ks["banned_families"]["negation"]["remaining"] == 5
    assert ks["niche_alert"] is True
    assert ks["niche_topic"] == "quiz"
    assert ks["niche_escape_remaining"] == 4
    assert rep["compliance_rejections"]["recent"] == 2
    assert rep["compliance_rejections"]["by_reason"] == \
        {"originality": 1, "fatigue": 1}
    dirs = rep["directives"]
    assert len(dirs) == 6
    assert any(d.startswith("AMPLIFIER") and "'question'" in d and
               "78.5%" in d for d in dirs)
    assert any(d.startswith("STOP") and "'number'" in d for d in dirs)
    assert any(d.startswith("EVITER") and "'negation'" in d for d in dirs)
    assert any(d.startswith("NICHE") and "quiz" in d for d in dirs)
    assert any(d.startswith("CONFORMITE") and "originality" in d for d in dirs)


def test_rejection_reason_string_fallback(make_cfg):
    """Compat : une cle "reason" (string) doit aussi etre agregee."""
    cfg = make_cfg()
    paths = _world(cfg)
    (paths.compliance / "rejected.jsonl").write_text(
        json.dumps({"idea_id": "i9", "reason": "fatigue: run=3/3"}) + "\n",
        encoding="utf-8")
    rep = W.weekly_report(cfg)
    assert rep["compliance_rejections"]["recent"] == 1
    assert rep["compliance_rejections"]["by_reason"] == {"fatigue": 1}


# --------------------------------------------------------------------------
# --apply : ban des familles de dead shorts (OPT-IN)
# --------------------------------------------------------------------------
def test_apply_directives_bans_dead_family(make_cfg):
    cfg = make_cfg()
    paths = _world(cfg)
    rep = W.weekly_report(cfg)
    applied = W.apply_directives(cfg, rep)
    assert applied == ["number"]
    doc = json.loads(
        (paths.optimizer / "killswitch.json").read_text(encoding="utf-8"))
    ban = doc["banned_families"]["number"]
    assert ban["remaining"] == 20
    assert ban["last_ids"] == ["v3"]
    assert ban["source"] == "weekly"
    assert doc["banned_families"]["negation"]["remaining"] == 5
    assert any("WEEKLY BAN number" in e for e in doc["events"])
    acts = (paths.weekly / "actions.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    row = json.loads(acts[-1])
    assert row["action"] == "ban_families"
    assert row["families"] == ["number"]
    assert row["ban_shorts"] == 20


def test_apply_directives_never_overwrites(make_cfg):
    """Un ban existant est conserve tel quel (l'optimizer decremente deja)."""
    cfg = make_cfg()
    paths = _world(cfg)
    ks = paths.optimizer / "killswitch.json"
    doc = json.loads(ks.read_text(encoding="utf-8"))
    doc["banned_families"]["number"] = {
        "remaining": 7, "banned_at": "2026-10-20T00:00:00+00:00",
        "last_ids": ["vX"]}
    ks.write_text(json.dumps(doc), encoding="utf-8")
    snapshot = ks.read_bytes()
    rep = W.weekly_report(cfg)
    assert W.apply_directives(cfg, rep) == []
    assert ks.read_bytes() == snapshot


def test_apply_directives_max_ban_families(make_cfg):
    cfg = make_cfg(**{"weekly": {"max_ban_families": 1}})
    _world(cfg)
    _add_video(cfg, "v6", views=0, likes=0, comments=0, days_ago=80,
               hook="Saving money is simple discipline",
               title="Simple money discipline")
    rep = W.weekly_report(cfg)
    # tri par anciennete decroissante : v6 (80 j) avant v3 (70 j)
    assert [r["video_id"] for r in rep["dead_shorts"]] == ["v6", "v3"]
    assert W.apply_directives(cfg, rep) == ["statement"]


# --------------------------------------------------------------------------
# run_weekly : ecriture rapport + history, opt-in du ban
# --------------------------------------------------------------------------
def test_run_weekly_no_apply_optin(make_cfg):
    cfg = make_cfg()
    paths = _world(cfg)
    ks = paths.optimizer / "killswitch.json"
    before = ks.read_bytes()
    out = W.run_weekly(cfg, apply=False)
    assert out["applied"] == []
    assert out["report"]["totals"]["videos"] == 5
    saved = json.loads(
        (paths.weekly / "report.json").read_text(encoding="utf-8"))
    assert saved["totals"]["videos"] == 5
    hist = (paths.weekly / "history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(hist) == 1
    h = json.loads(hist[0])
    assert h["days"] == 7
    assert h["top_ids"] == ["v1", "v4"]
    assert h["dead_ids"] == ["v3"]
    assert set(h) == {"generated", "days", "totals", "top_ids", "dead_ids"}
    assert ks.read_bytes() == before  # sans --apply, kill switch intact


# --------------------------------------------------------------------------
# Fenetres, CTA, render, cas vides
# --------------------------------------------------------------------------
def test_window_30_days(make_cfg):
    cfg = make_cfg()
    _world(cfg)
    rep = W.weekly_report(cfg, days=30)
    assert rep["totals"]["published_in_window"] == 3
    assert rep["totals"]["views_in_window"] == 1350


def test_auto_comment_global_gap(make_cfg):
    cfg = make_cfg(**{"upload": {"auto_comment": False}})
    _world(cfg)
    rep = W.weekly_report(cfg)
    scopes = {g["scope"] for g in rep["cta_gaps"]}
    assert scopes == {"global", "video"}  # global + v2 (0 comm / 10 j)
    assert any(d.startswith("CTA:") for d in rep["directives"])


def test_render(make_cfg):
    cfg = make_cfg()
    _world(cfg)
    text = W.render(W.weekly_report(cfg))
    assert "RAPPORT HEBDO" in text
    assert "v1" in text and "v4" in text
    assert "78.5%" in text
    assert "n/d" in text
    assert "v3" in text


def test_empty_world_insufficient(make_cfg):
    cfg = make_cfg()
    rep = W.weekly_report(cfg)
    assert rep["totals"]["videos"] == 0
    assert any(d.startswith("DONNEES INSUFFISANTES") for d in rep["directives"])


# --------------------------------------------------------------------------
# Helpers unitaires + fallbacks de config
# --------------------------------------------------------------------------
def test_units():
    assert W._parse_dt("2026-10-30T12:00:00Z") is not None
    naive = W._parse_dt("2026-10-30T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert W._parse_dt("not-a-date") is None
    assert W._parse_dt("") is None
    assert W._parse_dt(None) is None
    assert W._engagement_value(
        {"engaged_ratio": 78.5, "avg_view_pct": 70.0}) == 78.5
    assert W._engagement_value({"avg_view_pct": 41.2}) == 41.2
    assert W._engagement_value({"engaged_ratio": "x"}) is None
    assert W._engagement_value({}) is None
    assert W._engagement_value("x") is None
    assert W._int(None) == 0
    assert W._int("12") == 12
    assert W._int("abc") == 0
    assert W._int("abc", 9) == 9
    assert W._int(7.9) == 7


def test_days_fallback(make_cfg):
    cfg = make_cfg()
    _world(cfg)
    assert W.weekly_report(cfg, days="abc")["days"] == 7
    cfg2 = make_cfg(**{"weekly": {"days": "abc"}})
    _world(cfg2)
    assert W.weekly_report(cfg2)["days"] == 7
