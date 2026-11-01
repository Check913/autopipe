# APEX-PREDATOR (2026-11-01) : paquet auto-ameliorant d'AutoPipe

Nouveau paquet `autopipe/apex/` : boucle d'apprentissage 24/7 qui mesure le
veritable engagement des Shorts publies, apprend de ses gagnants et de ses
perdants, et reinjecte une directive d'apprentissage dans les prochaines
idees. Totalement additif, non fatal, desactivable, sans aucune ecriture hors
de `data/apex/` et du cache `cache/apex/`.

## 1. Le cycle en 6 temps (`apex.daemon.run_cycle`, tous non fatals)

1. **Metriques** : jointure memoire de hooks (lecture seule) x registre de
   publications x retention YouTube. Sans ligne mesuree, le daemon reste
   passif (sauf `--force`).
2. **Instantane de securite** : snapshot de l'etat courant avant tout
   changement (garde-fou de retour arriere).
3. **Post-mortem** : classe les publications mesurees en gagnants /
   perdants selon `apex.threshold`, met a jour le bandit de Thompson et la
   base de connaissances, et journalise chaque nouvelle publication apprise
   (JSONL avec rotation).
4. **Veille** (desactivee par defaut) : scrappage YouTube de nouveaux
   patterns de hooks gagnants vers la base de connaissances.
5. **Garde-fou de sante** : si la sante moyenne degrade de plus que
   `apex.degrade_pts` par rapport au record, restauration automatique du
   dernier etat connu bon.
6. **Injection** : directive d'apprentissage (anglais, dedoublonnee)
   concatenee a `ideas.style_directive` et `remake.style_directive`.

Le scheduler l'appelle a l'etape **1.99 APEX-PREDATOR**, apres le lot de
directives (optimizer 1.9 / research 1.95) car `inject()` concatene : la
directive apex doit arriver en dernier.

## 2. Modules

| Module | Role |
|---|---|
| `__init__.py` | Points d'entree non fatals a imports paresseux : `run`, `report`, `rollback_cmd`, `scrape` |
| `daemon.py` | Le cycle 24/7 : `run_cycle`, `journal`/`journal_file` (JSONL + rotation), `active`, `inject`, `report` |
| `metrics.py` | `measured_rows` (jointure memoire x registre x retention), `arms_for_row`, `fed_ids` |
| `post_mortem.py` | `analyze` : gagnants/perdants -> bandit + base de connaissances, et journalisation (point d'ecriture unique) |
| `bandit.py` | Bandit de Thompson : 8 bras en 3 groupes (famille / format / quiz) ; `summary` -> `{"updated", "arms"}` |
| `knowledge_base.py` | Base de hooks gagnants : `active_patterns`, `add_pattern`, `record_outcome`, `build_directive`, `load`, `save`, `path` |
| `rollback.py` | Instantanes/restauration : `snapshot`, `restore`, `health`, `maybe_rollback`, `list_snapshots` ; `KEEP = 10` |
| `scraper.py` | Veille yt-dlp des Shorts viraux, desactivee par defaut ; `maybe_scrape` retourne None si desactivee |

APIs existantes utilisees (aucune modifiee) : `autopipe.util`
(`get_logger`, `load_json`, `save_json`), `autopipe.config` (`get_config`,
`get_paths`), `autopipe.hooks.memory` (`build`, lecture seule),
`autopipe.identity` (`hook_family`), `autopipe.analytics`
(`engagement_value`). Les lignes de retention sont cles par `id`
(`data/analytics/retention_latest.json`) et le registre a la forme
`{"videos": {video_id: {...}}}`.

## 3. Configuration (nouveaux cles de `autopipe.toml`, valeurs par defaut)

| Cle | Defaut | Role |
|---|---|---|
| `apex.enabled` | `true` | active/desactive tout le paquet |
| `apex.threshold` | `70.0` | seuil d'engagement (%) au-dela duquel un Short est gagnant |
| `apex.retire_failures` | `10` | echecs consecutifs avant retrait d'un pattern de la base |
| `apex.degrade_pts` | `10.0` | points de sante (%) tolerees avant rollback |
| `apex.auto_rollback` | `true` | restauration automatique si la sante degrade |
| `apex.journal_max` | `3000` | rotation du journal (dernieres lignes JSONL) |
| `apex.scrape.enabled` | `false` | veille YouTube (zero reseau par defaut) |
| `apex.scrape.query` | `"shorts"` | requete de veille |
| `apex.scrape.min_views` | `1000000` | vues minimales du gagnant scrappe |
| `apex.scrape.max_duration` | `90` | duree maximale (secondes) |
| `apex.scrape.top_n` | `20` | nombre de resultats examines |
| `apex.scrape.max_patterns` | `10` | patterns max ajoutes par veille |
| `apex.scrape.cache_hours` | `72` | duree du cache de veille |

## 4. CLI (`python -m autopipe apex`)

```
autopipe apex                    # cycle manuel + rapport JSON (passif si aucune donnee)
autopipe apex --force            # force le cycle meme sans donnees reelles
autopipe apex --scrape           # veille de patterns uniquement (force le cache)
autopipe apex --rollback NOM     # restaure un instantane (dossier data/apex)
```

`--scrape` force la veille dans la copie en memoire de la config (rien n'est
persiste dans `autopipe.toml`). `--rollback NOM` sans nom utilise le
snapshot le plus recent.

## 5. Fichiers de donnees

- `data/apex/journal.jsonl` : journal d'apprentissage (une ligne par
  publication apprise, avec rotation `journal_max`) ;
- `data/apex/knowledge.json` : base de connaissances des patterns de hooks ;
- `data/apex/baseline.json` : record de sante et bandit persiste ;
- `data/apex/rollback/state.json` : etat du garde-fou (`best`, `restored`) ;
- `data/apex/rollback/` : instantanes (10 conserves, `KEEP = 10`) ;
- `cache/apex/` : cache de veille (`cache_hours`).

## 6. Garde-fous d'apprentissage

- **Apprentissage unique** : la journalisation est faite par
  `post_mortem.analyze` lui-meme (une ligne par publication apprise) ; le
  daemon ne rejournalise plus rien, donc un appel direct a `analyze()` ne
  peut pas reapprendre une publication deja connue.
- **Retour arriere de securite** : un snapshot avant chaque cycle + un
  rollback automatique si la sante moyenne passe sous le record moins
  `degrade_pts` ; message de restauration
  `restored:<nom> (sante <x> < record <y>)`.
- **Aucune exception remonte** au pipeline : chaque etape est isolee par un
  try/except avec WARN ; le paquet reste passif sans donnees reelles
  (sauf `--force`).
- **Note d'implementation** : le wrapper public s'appelle
  `rollback_cmd` (et non `rollback`) pour ne pas masquer le sous-module
  `autopipe.apex.rollback` lors d'un `from . import rollback` ; la CLI
  (`cmd_apex`) l'appelle de la meme facon.

## 7. Tests et integration

- `tests/test_apex_predator.py` : 15 tests, sans reseau, couvrant metriques,
  post-mortem, bandit (8 bras), base de connaissances, rollback/restauration,
  daemon (`run_cycle`, `inject`, rotation), veille desactivee, CLI et
  integrite du journal.
- Etat au 2026-11-01 : `15 passed` (apex) et `185 passed` (suite complete),
  `python -m compileall` OK.
- Integration additive : `scheduler.py` (etape 1.99), `cli.py`
  (sous-commande `apex`), `config.py` (cles `apex.*` + dossier `data/apex/`
  cree par `Paths.ensure`). Aucune ligne existante supprimee.
