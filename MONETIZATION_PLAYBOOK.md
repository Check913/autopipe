# MONETIZATION PLAYBOOK — AutoPipe (skill `autopipe-monetization-accelerator`)

> **Disclaimer** (identique à la page officielle YouTube) : « There are no
> guarantees under the YouTube partner agreement about how much or whether
> you'll be paid. » — Aucun bot ne garantit la monétisation. Ce playbook
> maximise les chances : il automatise la conformité et l'amplification,
> il ne remplace pas la qualité éditoriale.

Date de rédaction : 2026-10-31. Sources relues intégralement (URLs en bas).
Les seuils cités proviennent des pages d'aide officielles YouTube avec leur
date de dernière mise à jour (2026-07 → 2026-10).

---

## 1. Décisions d'implémentation (et pourquoi)

### D1 — Garde anti-démonétisation AVANT la génération (`compliance.py`)
**Décision :** tout passage par `ideas.py` / `remake.py` est filtré par
`enforce_compliance()` avec `mode = block` par défaut : une idée rejetée
n'atteint ni le TTS, ni le montage, ni l'upload.

**Politique visée :** « inauthentic content » (ex-repetitious content,
renommé le **15 juillet 2025**) : contenu produit en masse, générique,
répétitif ou manipulate = inéligible à la monétisation. La politique
s'applique « regardless of how the content was made » — donc aussi au
contenu assisté par IA.

**Contrôles implémentés :**
| Contrôle | Seuil par défaut | Raison politique |
|---|---|---|
| Originalité titre/hook (Jaccard mots) | `originality_max = 0.60` | pas de doublon du corpus publié |
| Originalité script (Jaccard tokens) | `script_originality_max = 0.65` | pas de resucée de script publié |
| Fatigue de format (même famille de hook N fois de suite) | `fatigue_max_consecutive = 3` (`0` désactive) | la répétition de format = signal « mass-produced » |
| Policy guard (sub4sub, compilation, giveaway, engagement bait, regex custom) | `banned_patterns = []` | politiques spam/contenu manipulate |
| Corpus minimum pour juger l'originalité | `corpus_min = 3` (en dessous → `INSUFFICIENT_DATA` loggé, pas de rejet injustifié) | fail-open contrôlé |

**Choix d'échec :** `mode = warn` sauvegarde l'idée en la marquant,
`mode = off` court-circuite tout. Une erreur interne → fail-open +
journal d'audit `data/compliance/defects.jsonl` (le pipeline ne meurt
jamais sur une panne de garde, mais tout est tracé).

### D2 — Divulgation IA déclarative (`upload.py` + `altered_content`)
**Décision :** `upload.altered_content = false` dans la config = la case
YouTube « Altered content / Synthetic media » n'est PAS cochée, parce que
les scripts AutoPipe sont écrits par le pipeline (contenu original), pas
générés pour usurper une personne réelle. `ai_disclosure_value()` lit la
config et `compliance` trace `ai_disclosure` dans chaque rapport : la
décision est documentée, reproductible, réversible (`true` si un jour les
scripts simulent des personnes réelles / événements réels).

**Politique visée :** la divulgation est requise quand le contenu donne
l'impression réaliste de personnes/événements réels (« realistic ») — pas
pour une narration éditoriale originale. YouTube confirme par ailleurs que
« channels that use AI in their content remain eligible to monetize »
(mise au point YPP de juillet 2025).

### D3 — Honnêteté de la donnée (`engagement_pct = None`)
**Décision :** sans mesure de rétention, `engagement_pct` vaut `None` et
s'affiche **`n/d`** — jamais un faux `0.0%`. Un faux 0% provoquerait une
décision d'amplification/killing basée sur une donnée inexistante.

### D4 — Rapport hebdo « winner amplification » (`weekly.py`)
**Décision :** chaque semaine, `python -m autopipe.cli weekly` fusionne
analytics + registre + rétention + patterns + kill switch + rejets de
conformité et produit des directives lisibles :
- **AMPLIFIER** : répliquer la famille de hook + format des top performers
  (dans la fenêtre de 7 jours ; top_n = 5).
- **STOP** : dead shorts = 0 vue après `dead_days = 60` → famille à bannir
  via `--apply` (OPT-IN, jamais automatique).
- **EVITER / NICHE** : rappel des bans kill switch et de l'alerte niche.
- **CONFORMITE** : résumé des rejets récents groupés par famille de raison.
- **DONNEES INSUFFISANTES** : si aucun top ni dead, lancer `analytics` +
  `optimize` d'abord (jamais de conseil fabriqué à partir de rien).
- **ALERTE 0 vue dans la fenêtre** : vérifier la fenêtre de publication
  14/18/22/02 UTC et la géo US.
- **CTA** : si `upload.auto_comment = false`, rappel de réactiver le
  commentaire épinglé (signal d'engagement, levier commentaires 2026).

**Choix de schéma :** `--apply` écrit UNIQUEMENT dans
`data/optimizer/killswitch.json` le schéma exact de
`optimizer.update_kill_switch` (`{"remaining": N, "banned_at": iso,
"last_ids": [...], "source": "weekly"}`). Un ban existant n'est **jamais**
écrasé (l'optimizer décrémente déjà) ; l'action est journalisée dans
`data/weekly/actions.jsonl` + `history.jsonl` (audit complet).

### D5 — Structure et format des Shorts
**Décision (déjà en place, rappelée par les directives) :** Shorts
verticaux ≤ 3 min (monétisables en Shorts feed depuis le 15 octobre 2024),
hook ≤ 60 caractères, A/B de hook avec familles DIFFÉRENTES
(question / number / negation / statement), fenêtres de publication
14/18/22/02 UTC, géo US. Le rapport hebdo utilise le format réel du
registre (`duration_format`) pour répliquer le bon format.

### D6 — Aucun appel réseau dans la garde et le rapport
**Décision :** `compliance` et `weekly` lisent uniquement les JSON/JSONL
locaux déjà produits par `analytics` / `optimize` / les gates. Un rapport
ne peut jamais faire tomber le pipeline par une panne API, et les tests
pytest tournent 100% offline.

### D7 — Traitement des seuils 2027 (prudence)
**Décision :** les changements du **1er février 2027** (voir §2) ne sont
PAS codés en dur comme des contrôles bloquants : ils concernent
l'éligibilité du canal, pas une vidéo individuelle. Le bot garde sa garde
qualitative (originalité/fatigue/policy) qui reste valide quel que soit le
palier. Les seuils 2027 seront relus sur les pages officielles avant
application (le bot affiche la date de consultation des sources ici-bas).

---

## 2. Seuils YouTube (sources officielles, dernière relecture : 2026-10-31)

### Palier « fan funding » (accès anticipé)
- 500 abonnés + 3 vidéos publiques dans les 90 derniers jours + (3 000 h de
  visionnage public sur 365 j **ou** 3 M de vues Shorts publiques sur 90 j)
- Débloque : memberships, Super Chat/Stickers/Thanks, Shopping.
- Source : https://support.google.com/youtube/answer/94522 (maj 2026-07-25)

### Palier « plein » (publicités + Premium + Shorts revenue sharing)
- 1 000 abonnés + (4 000 h de visionnage qualifiées sur 12 mois **ou**
  10 M de vues Shorts qualifiées sur 90 jours)
- Source : https://support.google.com/youtube/answer/72851 (maj 2026-10-08)

### Partage de revenus Shorts
- Accepter le « Shorts Monetization Module » → 45 % des revenus alloués de
  la pub vue entre les Shorts du feed (indépendant du watch page).
- Source : https://support.google.com/youtube/answer/12504220 (maj 2026-10-27)

### Changements au 1er février 2027 (⚠ à re-vérifier avant application)
- **Entrée YPP (nouveaux créateurs)** : 1 000 abonnés + (8 000 h qualifiées
  sur 365 j **ou** 20 M de vues Shorts qualifiées sur 90 j). Les créateurs
  déjà dans le YPP ne sont pas impactés.
- **Shorts Creator Pool** : pour toucher la pub + Premium du pool Shorts
  chaque mois, maintenir **10 M de vues Shorts qualifiées sur 90 jours**.
  En dessous, on reste dans le YPP et le long-form continue d'être payé.
- Sources :
  https://support.google.com/youtube/answer/12843009 (maj 2026-10-31) et
  https://support.google.com/youtube/blog/451804019 (2026-08-10)
- **Implication AutoPipe :** à partir de 2027, le rapport hebdo devient le
  tableau de bord de la « réserve de vues Shorts 90 j » — le suivi
  `totals.views_in_window` + `history.jsonl` donne déjà la trajectoire.

### Politique de contenu (ce que la garde bloque)
- « Inauthentic content » (ex-repetitious, 15/07/2025) : produit en masse,
  générique, répétitif, manipulate → inéligible. L'IA reste monétisable si
  le contenu est original et authentique.
- Sources :
  https://support.google.com/youtube/answer/1311392 (maj 2026-10-26) ;
  https://support.google.com/youtube/blog/356734251 (10/07/2025)

### Format
- Shorts ≤ 3 min depuis le 15/10/2024 ; au-delà d'une minute, un Short
  sous claim de copyright est bloqué globalement.
- Source : https://support.google.com/youtube/answer/15424877 (maj 2026-10-27)

---

## 3. Mapping décision → politique → code

| Décision | Politique YouTube | Code |
|---|---|---|
| D1 garde block + originalité + fatigue + policy | Monetization policies / inauthentic content | `autopipe/compliance.py`, branches `ideas.py`/`remake.py` |
| D2 divulgation déclarative | Altered/synthetic content disclosure | `autopipe/upload.py`, `[upload].altered_content` |
| D3 `n/d` sans rétention | Décisions basées données réelles | `weekly._engagement_value`, smoke test 2026-10-31 |
| D4 amplification + ban OPT-IN | Winner amplification, éviter la répétition | `autopipe/weekly.py` |
| D5 format Shorts ≤ 3 min + fenêtres UTC | Shorts monetization policies | `video.py`, `scheduler.py` (existant) |
| D6 zéro réseau dans la garde | Robustesse pipeline | `compliance.py`, `weekly.py` |
| D7 seuils 2027 en veille | Changements YPP 01/02/2027 | ce document, §2 |

## 4. Routine d'exploitation

```
python -m autopipe.cli weekly              # rapport (lecture seule)
python -m autopipe.cli weekly --apply      # OPT-IN : bannir familles des dead shorts
```
Le rapport est écrit dans `data/weekly/report.json`, historisé dans
`data/weekly/history.jsonl` (une ligne par run : totals, top_ids, dead_ids).

## 5. Sources officielles (toutes relues le 2026-10-31)

1. YPP overview & eligibility — https://support.google.com/youtube/answer/72851
2. Monetization features & paliers — https://support.google.com/youtube/answer/94522
3. YouTube channel monetization policies (inauthentic content) — https://support.google.com/youtube/answer/1311392
4. Blog YPP juillet 2025 (rename inauthentic content) — https://support.google.com/youtube/blog/356734251
5. Shorts monetisation policies (45 %, module) — https://support.google.com/youtube/answer/12504220
6. Changes to the YPP (février 2027) — https://support.google.com/youtube/answer/12843009
7. Blog annonces YPP 2026 — https://support.google.com/youtube/blog/451804019
8. Partner earnings overview (aucune garantie) — https://support.google.com/youtube/answer/72902
9. Three-minute Shorts — https://support.google.com/youtube/answer/15424877
