# APEX-PREDATOR (2026-11-01) : paquet auto-ameliorant d'AutoPipe

Nouveau paquet `autopipe/apex/` : boucle d'apprentissage 24/7 qui mesure le
veritable engagement des Shorts publies, apprend de ses gagnants et de ses
perdants, et reinjecte une directive d'apprentissage dans les prochaines
idees. Totalement additif, non fatal, desactivable, sans aucune ecriture hors
de `data/apex/`.

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
4. **Veille** : recherche yt-dlp multi-requetes de la vraie niche
   ("{mot-cle} #shorts", derivee de scout/research keywords comme le fait
   research.py) ; filtres progressifs (vues+duree, puis vues seules, puis
   aucun) ; cache seulement quand YouTube a repondu (un resultat vide
   retente au prochain cycle). Desactivee par defaut dans config.py,
   activee dans autopipe.toml.
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
| `bandit.py` | Bandit de Thompson : 8 bras en 3 groupes (famille / format / quiz) ; cold start obligatoire (`cold_start_pulls`, `cold_start(cfg, groupe)`, tirage cold-first dans `best`) ; `summary` -> `{"updated", "arms"}` avec `pulls` / `cold` / `cold_start` par bras |
| `knowledge_base.py` | Base de hooks gagnants : `active_patterns`, `add_pattern`, `record_outcome`, `build_directive`, `load`, `save`, `path` |
| `rollback.py` | Instantanes/restauration : `snapshot`, `restore`, `health`, `maybe_rollback`, `list_snapshots` ; `KEEP = 10` |
| `scraper.py` | Veille yt-dlp multi-requetes de niche (`#shorts`), filtres en echelle ; `maybe_scrape` : None si desactivee, cache seulement si resultats ; `pick_patterns` remplace chaque 2e titre "affirmation" par sa question synthetique (`Can You Answer These {sujet} ?`) pour casser la monoculture `family:statement` |

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
| `apex.cold_start_pulls` | `3` | essais forces par famille avant exploitation du bandit (0 = desactive) |
| `apex.degrade_pts` | `10.0` | points de sante (%) tolerees avant rollback |
| `apex.auto_rollback` | `true` | restauration automatique si la sante degrade |
| `apex.journal_max` | `3000` | rotation du journal (dernieres lignes JSONL) |
| `apex.scrape.enabled` | `false` (`true` dans autopipe.toml) | veille YouTube planifiee |
| `apex.scrape.query` | `""` | vide = auto : mots-cles de niche + `#shorts` |
| `apex.scrape.min_views` | `1000000` | vues minimales du gagnant scrappe |
| `apex.scrape.max_duration` | `120` | duree maximale (secondes) |
| `apex.scrape.top_n` | `20` | nombre de resultats examines |
| `apex.scrape.max_patterns` | `10` | patterns max ajoutes par veille |
| `apex.scrape.cache_hours` | `72` | duree du cache de veille |

## 4. CLI (`python -m autopipe apex`)

```
autopipe apex                    # cycle manuel + rapport JSON (passif si aucune donnee)
autopipe apex --force            # force le cycle meme sans donnees reelles
autopipe apex --scrape           # veille de patterns uniquement (force le cache)
autopipe apex --rollback NOM     # restaure un instantane (dossier data/apex)
autopipe apex -v ...             # log detaille (-v accepte aussi avant la sous-commande)
```

`--scrape` force la veille dans la copie en memoire de la config (rien n'est
persiste dans `autopipe.toml`). `--rollback NOM` sans nom utilise le
snapshot le plus recent. Si `--scrape` ajoute 0 pattern, la commande
affiche les requetes tentees et les causes possibles ; le log detaille
(cascade de filtres, erreurs yt-dlp) demande `-v`.

## 5. Fichiers de donnees

- `data/apex/journal.jsonl` : journal d'apprentissage (une ligne par
  publication apprise, avec rotation `journal_max`) ;
- `data/apex/knowledge.json` : base de connaissances des patterns de hooks ;
- `data/apex/baseline.json` : record de sante et bandit persiste ;
- `data/apex/rollback/state.json` : etat du garde-fou (`best`, `restored`) ;
- `data/apex/rollback/` : instantanes (10 conserves, `KEEP = 10`) ;
- `data/apex/scraper_last.json` : cache de la derniere veille ; une
  reponse YouTube n'est re-frappee qu'apres `cache_hours`, et un
  resultat vide n'est jamais mis en cache (retente au prochain cycle).

## 6. Garde-fous d'apprentissage

- **Apprentissage unique** : la journalisation est faite par
  `post_mortem.analyze` lui-meme (une ligne par publication apprise) ; le
  daemon ne rejournalise plus rien, donc un appel direct a `analyze()` ne
  peut pas reapprendre une publication deja connue.
- **Retour arriere de securite** : un snapshot avant chaque cycle + un
  rollback automatique si la sante moyenne passe sous le record moins
  `degrade_pts` ; message de restauration
  `restored:<nom> (sante <x> < record <y>)`.
- **Cold start obligatoire** : chaque famille doit etre tiree
  `cold_start_pulls` fois (3 par defaut) avant que `best()` exploite le
  bandit ; les tirages cold suivent l'ordre `(pulls, nom)` (rotation
  deterministe entre les bras froids), puis retour au mode Thompson
  classique. `cold_start_pulls = 0` desactive le mode (utilise par les
  tests greedy historiques).
- **Diversite des veilles** : `pick_patterns` transforme chaque 2e titre
  sans famille speciale (statement) en question synthetique deterministe
  (remplacement, pas d'ajout : pas de doublon quasi identique en base).
  Un scrape 100% statement alimente donc desormais 2 bras (`statement` et
  `question`) des le premier passage.
- **Aucune exception remonte** au pipeline : chaque etape est isolee par un
  try/except avec WARN ; le paquet reste passif sans donnees reelles
  (sauf `--force`).
- **Note d'implementation** : le wrapper public s'appelle
  `rollback_cmd` (et non `rollback`) pour ne pas masquer le sous-module
  `autopipe.apex.rollback` lors d'un `from . import rollback` ; la CLI
  (`cmd_apex`) l'appelle de la meme facon.

## 7. Tests et integration

- `tests/test_apex_predator.py` : 26 tests, sans reseau, couvrant metriques,
  post-mortem, bandit (8 bras), base de connaissances, rollback/restauration,
  daemon (`run_cycle`, `inject`, rotation), veille desactivee, CLI et
  integrite du journal. Ajoutes le 2026-11-01 : derivation des requetes
  `"{mot-cle} #shorts"` (`_queries`), echelle de filtres (`_filter_entries`)
  et veille sans candidat (0 resultat : aucun cache ecrit). Ajoutes le
  2026-11-01 (cold start + diversite) : rotation cold start du bandit,
  desactivation `cold_start_pulls = 0`, helper `cold_start(cfg, groupe)`,
  `summary` avec `pulls`/`cold`/`cold_start`, question synthetique
  (`_synth_question`, cas limites inclus), `pick_patterns` diversifie
  (10 titres statement -> 5 statements + 5 questions), cas mixte
  (familles non statement passees sans transformation) et veille
  multi-familles (100% statement -> 2 bras en base).
- Etat au 2026-11-01 : `26 passed` (apex) et `196 passed` (suite complete),
  `python -m compileall` OK. Run reel `apex --scrape` du 2026-11-01 :
  15 candidats retenus sur 148 resultats, 6 nouveaux patterns (2
  `family:number`, 2 `family:statement`, 2 questions synthetiques
  `family:question`) ; la base passe de 2 familles (10 patterns) a 3
  familles (16 patterns) et le rapport apex expose l'etat cold start de
  tous les bras (`pulls: 0`, `cold: true`).
- Integration additive : `scheduler.py` (etape 1.99), `cli.py`
  (sous-commande `apex`), `config.py` (cles `apex.*` + dossier `data/apex/`
  cree par `Paths.ensure`). Aucune ligne existante supprimee.
