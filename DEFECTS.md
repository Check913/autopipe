# DEFECTS — AutoPipe (audit `autopipe-monetization-accelerator`, 2026-10-31)

État du dossier : 3 défauts réels trouvés **et corrigés** dans les nouveaux
modules ; la suite complète est verte (130 de base + 40 nouveaux) ; la
couverture des modules critiques dépasse le contrat (≥ 80 %).

---

## 1. Défauts corrigés (vérifiés + tests)

### D-01 `compliance.py` — fatigue de format : `0` ne désactivait pas
- **Symptôme :** le champ `"fatigued"` était calculé `run >= max_consec`.
  Avec la valeur documentée `fatigue_max_consecutive = 0` (« 0 désactive »),
  `run >= 0` est toujours vrai → **toutes** les idées étaient rejetées.
- **Correctif :** `max_consec > 0 and run >= max_consec` (fail-open
  contrôlé quand la garde fatigue est désactivée).
- **Test :** `tests/test_compliance.py::test_fatigue_zero_disables`

### D-02 `weekly.py` — mauvais champ de lecture des rejets (`reason` vs `reasons`)
- **Symptôme :** le rapport hebdo lisait `rr.get("reason")` mais
  `compliance.rejected.jsonl` écrit une liste `"reasons"` → la section
  CONFORMITE restait vide quel que soit le volume de rejets.
- **Correctif :** lecture de la liste `reasons`, groupement par préfixe
  avant `":"`, repli (fallback) sur une chaîne `reason` seule. Ajout du
  test `test_rejection_reason_string_fallback`.
- **Test :** `tests/test_weekly.py::test_rejection_reason_string_fallback`

### D-03 `weekly.py` — directive STOP avec un point d'entrée inexistant
- **Symptôme :** la directive disait `python -m autopipe weekly --apply`,
  mais le paquet n'a **pas** de `__main__.py` → la commande échoue
  (`No module named autopipe.__main__`).
- **Correctif :** `python -m autopipe.cli weekly --apply`, conformément au
  point d'entrée réel du README (lignes 15 et 59-62 : `python -m autopipe.cli <cmd>`).
- **Vérification :** `ls autopipe/__main__.py` → inexistant ; grep des
  directives → plus aucune occurrence de `python -m autopipe ` nu.

### D-00 (correction antérieure, déjà commitée) `engagement_pct` honnête
- Sans données de rétention, le rapport affichait `0.0%` au lieu de `n/d`
  → corrigé en `None` (affiché `n/d`). Jamais de donnée fabriquée.

---

## 2. Preuves tests / couverture (pytest 9.1.1, pytest-cov 7.1.0)

- `tests/test_compliance.py` (366 lignes) + `tests/test_weekly.py` :
  **40 passed in 1.47s** au premier run, 0 échec.
- Couverture modules critiques (contrat ≥ 80 %) :
  - `autopipe/compliance.py` : 172 stmts, 12 manqués → **93 %**
  - `autopipe/weekly.py` : 255 stmts, 29 manqués → **89 %**
  - TOTAL (2 modules) : 427 stmts, 41 manqués → **90 %**
- Lignes restantes non couvertes : branches d'erreur bénignes
  (`INSUFFICIENT_DATA`, `log_defect`, replis de types) — couverture
  volontairement non gonflée par des tests de mock.

---

## 3. Lacunes / limites connues (statut)

| Élément | Statut |
|---|---|
| Modules `scrapers/*` du skill | **absents** — hors périmètre de ce push ; les entrées viennent de `scout.py`/`research.py` |
| `algorithm_watch` (veille algorithme) | **absent** — remplacé par `weekly.py` (directives hebdo) + veille manuelle des sources du playbook |
| `loop_builder` / `rehook` / `pacing` | **absents** — le hook A/B (`hook_ab.py`) et les fenêtres UTC restent le seul levier de rythme |
| Modules `seo/*` (titres/descriptions/hashtags) | **absents** — `metas.py` existant non modifié |
| `niche_selector` | **absent** — la niche reste dans `identity.py` (keywords/anti_topics) + alerte niche kill switch |
| `__main__.py` | **inexistant** — invocation réelle : `python -m autopipe.cli <cmd>` |
| Rétention absente | `engagement_pct = None` → `n/d` (jamais de faux 0.0 %) |
| Seuils YPP du 01/02/2027 | **non appliqués dans le code** (décision D7 du playbook : ils concernent l'éligibilité du canal, pas une vidéo) — à relire sur les sources avant application |
| Avertissement pip `invalid distribution "-ip"` | bénin (installation `pip` antérieure corrompue dans `C:\Python310\Lib\site-packages`), sans impact sur pytest |
| `coverage.exe.deleteme` (WinError 2) au 1er `pip install pytest-cov` | résolu au 2e essai ; `pytest_cov 7.1.0` importable |
| `tests_backup_20261030/` fait échouer la collecte pytest (basenames de tests dupliqués avec `tests/`) | lancer `python -m pytest -q --ignore=tests_backup_20261030` — dossier de sauvegarde exclu du push, supprimable manuellement |

## 4. Périmètre du push (décision)

Commit : code + docs + tests (`autopipe/*.py` modifiés, `config.example.toml`,
`autopipe.toml`, `tests/test_compliance.py`, `tests/test_weekly.py`,
`MONETIZATION_PLAYBOOK.md`, `DEFECTS.md`).

Exclu (non suivi, non commité) : `autopipe_backup_20261030/`,
`tests_backup_20261030/`, `brand_kit/`, `data/ideas/*.json`,
`PASSER_PRODUCTION_GOOGLE_20261030.md`, `autopipe.toml.bak-20261030`.
