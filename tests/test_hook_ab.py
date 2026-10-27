"""Tests du tracker A/B hooks : assign -> attach -> summarize -> best_family."""
import json

from autopipe import hook_ab
from autopipe.config import get_paths

VARIANTS = [
    {"variant": "A", "hook": "Why is this trench so strange?", "family": "question"},
    {"variant": "B", "hook": "3 things about this trench", "family": "number"},
]


def _seed(tmp_path, make_cfg, n_pairs=6):
    """Genere n_pairs paires alternees A/B rattachees a vid1..vidN."""
    cfg = make_cfg()
    for i in range(1, n_pairs + 1):
        chosen = hook_ab.assign("topic %d" % i, VARIANTS, cfg)
        assert chosen and chosen["pair_id"]
        assert hook_ab.attach_video(chosen["pair_id"], "vid%d" % i, cfg)
    # Retention : A ~80 %, B ~50 % d'engagement (echelle 0-100).
    rows = [{"id": "vid%d" % i, "views": 100,
             "engaged_ratio": 80.0 if i % 2 == 1 else 50.0}
            for i in range(1, n_pairs + 1)]
    paths = get_paths(cfg)
    (paths.analytics / "retention_latest.json").write_text(
        json.dumps({"rows": rows}), encoding="utf-8")
    return cfg


def test_assign_alternance_et_fichier(tmp_path, make_cfg):
    cfg = make_cfg()
    a = hook_ab.assign("t1", VARIANTS, cfg)
    b = hook_ab.assign("t2", VARIANTS, cfg)
    assert a["variant"] == "A" and b["variant"] == "B"
    assert a["pair_id"] != b["pair_id"]
    f = get_paths(cfg).data / "hook_ab" / "assignments.jsonl"
    assert f.exists() and len(f.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_assign_desactive(make_cfg):
    cfg = make_cfg(hook_ab={"enabled": False})
    assert hook_ab.assign("t", VARIANTS, cfg) is None


def test_assign_variants_invalides(make_cfg):
    cfg = make_cfg()
    assert hook_ab.assign("t", [], cfg) is None
    assert hook_ab.assign("t", [{"variant": "A"}], cfg) is None


def test_attach_video_inconnu(make_cfg):
    assert hook_ab.attach_video("pair_x", "vid", make_cfg()) is False


def test_summarize_joint_engagement(tmp_path, make_cfg):
    cfg = _seed(tmp_path, make_cfg)
    doc = hook_ab.summarize(cfg)
    assert doc["status"] == "ok"
    assert doc["pairs"] == 6 and doc["joined"] == 6
    a, b = doc["variants"]["A"], doc["variants"]["B"]
    assert a["measured"] == 3 and b["measured"] == 3
    assert a["mean_engagement"] == 80.0
    assert b["mean_engagement"] == 50.0
    # Le summary.json est ecrit.
    assert (get_paths(cfg).data / "hook_ab" / "summary.json").exists()


def test_summarize_sans_assignments(make_cfg):
    doc = hook_ab.summarize(make_cfg())
    assert doc["status"] == "no_assignments"


def test_best_family_seuil(tmp_path, make_cfg):
    cfg = _seed(tmp_path, make_cfg)
    # 3 mesures par variante, ecart 30 pts -> famille "question" (A) gagne.
    assert hook_ab.best_family(cfg) == "question"


def test_best_family_insuffisant(tmp_path, make_cfg):
    # 1 paire seulement : sous le seuil de 3 mesures/variante.
    cfg = _seed(tmp_path, make_cfg, n_pairs=2)
    assert hook_ab.best_family(cfg) is None


def test_best_family_ecart_trop_faible(tmp_path, make_cfg):
    cfg = _seed(tmp_path, make_cfg)
    # Ecrase la retention : ecart < 5 pts -> pas de decision.
    paths = get_paths(cfg)
    rows = [{"id": "vid%d" % i, "views": 100, "engaged_ratio": 75.0}
            for i in range(1, 7)]
    (paths.analytics / "retention_latest.json").write_text(
        json.dumps({"rows": rows}), encoding="utf-8")
    assert hook_ab.best_family(cfg) is None