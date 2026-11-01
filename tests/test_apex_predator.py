# -*- coding: utf-8 -*-
"""Tests du paquet apex (APEX-PREDATOR) : metriques reelles, bandit de
Thompson, base de connaissances, post-mortem, rollback, daemon et CLI.
Tous les tests sont hors reseau (yt-dlp jamais importe).
"""
import json

import pytest

from autopipe import apex
from autopipe.apex import bandit, daemon, knowledge_base, metrics, post_mortem
from autopipe.apex import rollback, scraper
from autopipe.config import get_paths

# Registre : vid1 gagnant (question, sans quiz), vid2 perdant (number, quiz).
REG = {
    "videos": {
        "vid1": {"hook": "Why is this trench so strange?",
                 "hook_family": "question", "duration_format": "short",
                 "quiz": False, "published_at": "2026-10-20T12:00:00"},
        "vid2": {"hook": "3 things about this trench",
                 "hook_family": "number", "duration_format": "short",
                 "quiz": True, "published_at": "2026-10-21T12:00:00"},
    }
}
# Retention : engagees reelles (engaged_source = engagedViews).
ROWS = [
    {"id": "vid1", "views": 100, "engagedViews": 83,
     "engaged_ratio": 83.0, "engaged_source": "engagedViews"},
    {"id": "vid2", "views": 100, "engagedViews": 45,
     "engaged_ratio": 45.0, "engaged_source": "engagedViews"},
]


def _seed(cfg):
    """Ecrit le registre et la retention, comme le ferait le pipeline."""
    paths = get_paths(cfg)
    (paths.registry / "published.json").write_text(
        json.dumps(REG), encoding="utf-8")
    (paths.analytics / "retention_latest.json").write_text(
        json.dumps({"rows": ROWS}), encoding="utf-8")
    return cfg


def test_metrics_lignes_reelles(make_cfg):
    cfg = _seed(make_cfg())
    rows = metrics.measured_rows(cfg)
    assert len(rows) == 2
    r1 = [r for r in rows if r["video_id"] == "vid1"][0]
    assert r1["reward"] == pytest.approx(0.83)
    assert r1["hook_family"] == "question"
    assert r1["quiz"] == "off"
    assert r1["duration_format"] == "short"
    assert r1["views"] == 100
    r2 = [r for r in rows if r["video_id"] == "vid2"][0]
    assert r2["reward"] == pytest.approx(0.45)
    assert r2["hook_family"] == "number" and r2["quiz"] == "on"


def test_metrics_mode_degrade(make_cfg):
    cfg = make_cfg()
    assert metrics.measured_rows(cfg) == []
    assert metrics.fed_ids(cfg) == set()
    # Echelle 0-100 acceptee aussi bien que l'echelle 0-1.
    rows = metrics.measured_rows(_seed(make_cfg()))
    assert all(0.0 <= r["reward"] <= 1.0 for r in rows)


def test_bandit_update_et_best(make_cfg):
    cfg = make_cfg()
    bandit.update(cfg, ["family:question", "format:short"], 0.9)
    bandit.update(cfg, ["family:number", "format:short"], 0.1)
    doc = bandit.load(cfg)
    assert doc["arms"]["family:question"]["alpha"] == pytest.approx(1.9)
    assert doc["arms"]["family:question"]["beta"] == pytest.approx(1.1)
    fid, fm = bandit.best(cfg, "family")
    assert fid == "family:question"
    assert fm > bandit.mean(doc["arms"]["family:number"])
    # Groupe quiz jamais mis a jour : prior Beta(1,1) intact.
    qid, qm = bandit.best(cfg, "quiz")
    assert qid == "quiz:on" and qm == pytest.approx(0.5)


def test_bandit_recompense_hors_bornes(make_cfg):
    cfg = make_cfg()
    bandit.update(cfg, ["family:question"], 5.0)
    doc = bandit.load(cfg)
    assert doc["arms"]["family:question"]["alpha"] == pytest.approx(2.0)
    assert doc["arms"]["family:question"]["beta"] == pytest.approx(1.0)
    assert bandit.update(cfg, [], 0.5) is None


def test_kb_patterns_et_directive(make_cfg):
    cfg = make_cfg()
    doc = knowledge_base.load(cfg)
    assert knowledge_base.add_pattern(cfg, doc, "Why is X so strange?",
                                      "family:question")
    # Dedoublonnage insensible a la casse et aux espaces.
    assert not knowledge_base.add_pattern(cfg, doc, "why is x so strange?",
                                          "family:question")
    knowledge_base.record_outcome(doc, ["family:question"], 0.9, threshold=0.7)
    directive = knowledge_base.build_directive(cfg, doc)
    assert directive.startswith("Learn from these hooks")
    assert "Why is X so strange?" in directive
    # Retrait doux : retire_failures=1 -> pattern desactive, directive vide.
    doc2 = knowledge_base.load(cfg)
    assert knowledge_base.add_pattern(cfg, doc2, "3 X facts they hide",
                                      "family:number")
    knowledge_base.record_outcome(doc2, ["family:number"], 0.1,
                                  threshold=0.7, retire_failures=1)
    assert knowledge_base.active_patterns(doc2) == []
    assert knowledge_base.build_directive(cfg, doc2) == ""
    knowledge_base.save(cfg, doc)
    assert (get_paths(cfg).apex / "knowledge.json").exists()


def test_post_mortem_apprend_gagnant(make_cfg):
    cfg = _seed(make_cfg())
    res = post_mortem.analyze(cfg)
    assert res["learned"] == 2
    assert res["winners"]["hook_family"] == {"question": 1}
    assert res["losers"]["hook_family"] == {"number": 1}
    assert bandit.best(cfg, "family")[0] == "family:question"
    # Le hook gagnant a ete integre a la base AVEC sa victoire.
    pats = knowledge_base.load(cfg)["patterns"]
    assert len(pats) == 1
    assert pats[0]["text"] == "Why is this trench so strange?"
    assert pats[0]["wins"] == 1 and pats[0]["source"] == "post_mortem"


def test_post_mortem_pas_de_double_apprentissage(make_cfg):
    cfg = _seed(make_cfg())
    post_mortem.analyze(cfg)
    daemon.run_cycle(cfg)  # journalise vid1/vid2
    res2 = post_mortem.analyze(cfg)
    assert res2["learned"] == 0
    assert len(knowledge_base.load(cfg)["patterns"]) == 1
    q = bandit.load(cfg)["arms"]["family:question"]
    assert q["alpha"] == pytest.approx(1.0 + 0.83)  # une seule mise a jour


def test_rollback_instantane_et_restauration(make_cfg):
    cfg = make_cfg()
    bandit.update(cfg, ["family:question"], 0.9)
    name = rollback.snapshot(cfg, label="test")
    assert name and rollback.list_snapshots(cfg) == [name]
    bandit.update(cfg, ["family:question"], 0.0)
    assert bandit.best(cfg, "family")[0] == "family:number"
    assert rollback.restore(cfg, name) == name
    assert bandit.best(cfg, "family")[0] == "family:question"
    assert rollback.restore(cfg, "inconnu.json") is None


def test_rollback_rotation(make_cfg):
    cfg = make_cfg()
    for i in range(rollback.KEEP + 3):
        assert rollback.snapshot(cfg, label="r%d" % i)
    assert len(rollback.list_snapshots(cfg)) == rollback.KEEP


def test_health_et_rollback_automatique(make_cfg):
    cfg = _seed(make_cfg())
    assert rollback.health(cfg) == pytest.approx(0.64)  # (0.83 + 0.45) / 2
    assert rollback.maybe_rollback(cfg).startswith("baseline:0.640")
    # Degrade de >= degrade_pts vis-a-vis du record -> restauration.
    (rollback.baseline_file(cfg)).write_text(
        json.dumps({"best": 0.9}), encoding="utf-8")
    before = len(rollback.list_snapshots(cfg))
    msg = rollback.maybe_rollback(cfg)
    assert msg.startswith("restored:")
    assert len(rollback.list_snapshots(cfg)) == before + 1
    doc = json.loads(rollback.baseline_file(cfg).read_text(encoding="utf-8"))
    assert doc["restored"] is True
    # Pas de double restauration sur le meme etat degrade.
    assert rollback.maybe_rollback(cfg) is None


def test_daemon_cycle_complet(make_cfg):
    cfg = _seed(make_cfg())
    cfg["ideas"], cfg["remake"] = {}, {}
    res = daemon.run_cycle(cfg)
    assert res["active"] is True
    assert res["rows"] == 2 and res["learned"] == 2
    assert res["scraped"] is None  # veille desactivee par defaut
    lines = [json.loads(l)
             for l in daemon.journal_file(cfg).read_text(encoding="utf-8")
             .splitlines()]
    assert [l["video_id"] for l in lines] == ["vid1", "vid2"]
    assert lines[0]["reward"] == pytest.approx(0.83)
    directive = res["directive"]
    assert directive.startswith("Learn from these hooks")
    assert cfg["ideas"]["style_directive"] == directive
    assert cfg["remake"]["style_directive"] == directive
    assert daemon.inject(cfg, directive) is False  # anti-doublon
    res2 = daemon.run_cycle(cfg)
    assert res2["active"] is True and res2["learned"] == 0


def test_daemon_passif_sans_donnees(make_cfg):
    cfg = make_cfg()
    assert daemon.active(cfg) is False
    assert daemon.run_cycle(cfg) == {"active": False,
                                     "reason": "aucune donnee reelle"}
    cfg2 = make_cfg(apex={"enabled": False})
    assert daemon.run_cycle(cfg2) == {"active": False,
                                      "reason": "desactive"}
    assert daemon.run_cycle(cfg, force=True)["active"] is True


def test_scraper_patterns_et_cache(make_cfg, monkeypatch):
    cfg = make_cfg(apex={"scrape": {"enabled": True, "cache_hours": 72,
                                    "max_patterns": 10, "query": "q"}})
    entries = [
        {"id": "a", "title": "Why is X so strange?",
         "view_count": 2000000, "duration": 45},
        {"id": "b", "title": "3 things about X",
         "view_count": 1500000, "duration": 30},
        {"id": "c", "title": "dup", "view_count": 100, "duration": 5},
        {"id": "d", "title": "dup", "view_count": 900, "duration": 5},
        {"id": "e", "title": "", "view_count": 10, "duration": 5},
    ]
    assert scraper.pick_patterns(entries) == [
        ("Why is X so strange?", "family:question"),
        ("3 things about X", "family:number"),
        ("dup", "family:statement"),
    ]
    monkeypatch.setattr(scraper, "search",
                        lambda cfg=None, query=None: entries)
    assert scraper.maybe_scrape(cfg, force=True) == 3
    assert (get_paths(cfg).apex / "scraper_last.json").exists()
    assert len(knowledge_base.load(cfg)["patterns"]) == 3
    assert scraper.maybe_scrape(cfg) == 0  # cache frais
    assert scraper.maybe_scrape(make_cfg()) is None  # veille desactivee


def test_wrappers_non_fataux(make_cfg, monkeypatch):
    cfg = make_cfg()
    rep = apex.report(cfg)
    assert rep["active"] is False and rep["rows"] == 0
    assert rep["journal_lines"] == 0 and rep["snapshots"] == []
    assert rep["knowledge_patterns"] == 0 and rep["knowledge_winners"] == 0
    assert rep["health"] is None and len(rep["bandit"]) == 8
    assert apex.run(cfg)["active"] is False
    assert apex.rollback_cmd(cfg) is None
    assert apex.scrape(cfg) is None
    # Toute exception interne reste captive (design preventif).
    monkeypatch.setattr(
        daemon, "run_cycle",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert apex.run(cfg) == {"active": False, "error": "exception"}


def test_cli_apex_parse(capsys):
    # L'aide exit(0) avant toute execution : valide le cablage argparse
    # (cmd_apex + --force/--scrape/--rollback) sans toucher a la config.
    from autopipe import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["apex", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--rollback" in out and "--scrape" in out and "--force" in out


def test_scraper_queries_niche(make_cfg):
    # Derivation des requetes : la vraie niche (scout/research keywords
    # + suffixe #shorts, format prouve par research.py) plutot que la
    # requete generique "shorts" (compilations longues).
    cfg = make_cfg(scout={"keywords": ["amazing facts", "  ", "quiz"]},
                   research={"keywords": ["quiz", "trivia"]})
    scfg = scraper._scrape_cfg(cfg)
    assert scraper._queries(cfg, scfg) == [
        "amazing facts #shorts", "quiz #shorts", "trivia #shorts"]
    # La valeur passee gagne toujours (str ou liste, dedoublonnee).
    assert scraper._queries(cfg, scfg, "manual") == ["manual"]
    assert scraper._queries(cfg, scfg, ["a", "a", "b"]) == ["a", "b"]
    # query = "shorts" (litteral ou vide) retombe sur la niche auto.
    assert scraper._queries(cfg, scfg, "shorts") == [
        "amazing facts #shorts", "quiz #shorts", "trivia #shorts"]
    vide = scraper._scrape_cfg({"apex": {"scrape": {"query": "shorts"}}})
    cfg_vide = {"scout": {"keywords": []}, "research": {}}
    assert scraper._queries(cfg_vide, vide, None) == ["shorts"]
    # Limite _MAX_QUERIES : 4 keywords -> 3 requetes max.
    cfg4 = make_cfg(scout={"keywords": ["a", "b", "c", "d"]})
    assert scraper._queries(cfg4, scfg) == ["a #shorts", "b #shorts",
                                            "c #shorts"]


def test_scraper_filter_ladder(make_cfg):
    # Echelle de filtres : strict (vues+duree), vues seules, aucun.
    raw = [
        {"id": "viral", "title": "Why did they stop?",
         "view_count": 2000000, "duration": 60},
        {"id": "ok", "title": "Number one secret", "view_count": 900,
         "duration": 30},
        {"id": "long", "title": "Top 100 compilation",
         "view_count": 5000, "duration": 3600},
        {"id": "nodur", "title": "The truth about X", "view_count": 10},
        {"id": None, "title": "no id", "view_count": 5},
        {"id": "empty", "title": "   ", "view_count": 5},
    ]
    strict = scraper._filter_entries(raw, 100, 120)
    assert [e["id"] for e in strict] == ["viral", "ok"]
    vues = scraper._filter_entries(raw, 100, 0)   # duree desactivee
    assert [e["id"] for e in vues] == ["viral", "long", "ok"]
    aucun = scraper._filter_entries(raw, 0, 0)
    assert [e["id"] for e in aucun] == ["viral", "long", "ok", "nodur"]
    # Tri par vues decroissantes + url de secours depuis l'id.
    assert strict[0]["views"] >= strict[1]["views"]
    assert strict[0]["url"].startswith(
        "https://www.youtube.com/watch?v=")


def test_scraper_cache_non_ecrit_si_vide(make_cfg, monkeypatch):
    # 0 entree brute : rien n'est mis en cache, le prochain cycle
    # retente vraiment (pas de cache "0 pattern" bloquant).
    cfg = make_cfg(apex={"scrape": {"enabled": True, "cache_hours": 72}})
    calls = []
    monkeypatch.setattr(scraper, "search",
                        lambda *a, **k: calls.append(1) or [])
    assert scraper.maybe_scrape(cfg, force=True) == 0
    assert not (get_paths(cfg).apex / "scraper_last.json").exists()
    assert scraper.maybe_scrape(cfg) == 0
    assert len(calls) == 2
