# Analytics AutoPipe : metriques et tokens (a lire avant de toucher a OAuth)

## Pourquoi DEUX tokens OAuth

AutoPipe utilise **deux flux OAuth distincts**, jamais interchangeables :

| Token | Fichier | Scope | Servi par |
|---|---|---|---|
| Upload | `token.json` | `youtube.upload` (+ `youtube.readonly`) | Data API v3 : upload, analytics basiques, commentaires |
| Analytics | `*.analytics.token.json` | `yt-analytics.readonly` | Analytics API v2 : `engagedViews`, `averageViewPercentage` |

- `autopipe analytics` lit les stats Data API sans analytics-auth.
- `autopipe analytics-auth` autorise UNE FOIS le token Analytics (metriques
  de retention). Il ne touche jamais au token d'upload.
- **Si on melange les deux** : le token upload n'a pas `yt-analytics.readonly`
  -> 403 sur les requetes Analytics ; le token analytics ne peut pas uploader
  -> upload.py casse. Les fichiers sont separes justement pour eviter un
  re-consentement qui invaliderait l'autre flux.

## Regenerer un token

1. Supprimer le fichier token concerne (`token.json` ou `*.analytics.token.json`).
2. Relancer la commande qui le demande (`autopipe upload ...` ou
   `autopipe analytics-auth`) : le navigateur s'ouvre, se reconnecter.
3. Verifier avec `autopipe check-token`.

## engagedViews : la vraie metrique de retention Shorts

Depuis le **31 mars 2025**, YouTube compte une "vue" Shorts des le demarrage
(swipe instantane inclus). `views` ne mesure donc plus l'interet, et
`averageViewPercentage` seul est trompeur (il ne dit rien de ceux qui swipe
avant 2 s).

Le proxy correct de Studio "Viewed vs Swiped Away" est :

```
engagement = engagedViews / views          # cible >= 70 %
```

- `engagedViews` = vues qui restent engagees (core metric Analytics API v2).
- AutoPipe calcule ce ratio dans `analytics.engagement_value(row)` et
  replie sur `averageViewPercentage` si `engagedViews` est absent (vieilles
  lignes / quota). Les deux sont croises dans `data/analytics/retention_latest.json`.
- Comparer les Shorts par tranche de duree (13 s vs 60 s), jamais entre
  formats : la retention naturelle n'y est pas comparable.
- Pas de seuil universel officiel : 70 % d'engaged est la cible interne
  (`analytics.engaged_target_pct`), le loop rate est un signal positif.

## Hook A/B (V2)

`data/hook_ab/assignments.jsonl` : deux variantes (A/B) par sujet, chacune
reliée à son `video_id` apres upload. `autopipe hook-ab` agrege
`variant -> engagement` et `hook_ab.best_family` retient une famille quand
il y a >= 3 mesures par variante et >= 5 points d'ecart. L'optimizer injecte
alors la famille gagnante comme directive dans les prompts suivants.

Fichiers :
- `data/hook_ab/assignments.jsonl` - variantes + video_id (append-only)
- `data/hook_ab/summary.json` - stats par variante (regenere par `autopipe hook-ab`)

## Dette connue / limites

- Le token Analytics expire ~ toutes les 7 jours (refresh token OK sauf
  revoquation manuelle) ; l'alerte Gmail "re-autorisation" couvre ce cas.
- `engagedViews` n'existe pas dans la Data API v3 : uniquement Analytics v2,
  donc analytics-auth est requis pour la metrique fine. Sans lui, on retombe
  sur `averageViewPercentage` (moins juste mais disponible).

## V3 (2026-10-27) - alignement formats & specificite

- **Semantique engagedViews corrigee** : une "engaged view" Shorts = une
  lecture qui continue apres la PREMIERE FRAME (doc YouTube, revision du
  31/03/2025). Ce n'est PAS "actif a ~30 s" (ancien commentaire faux).
- **Budgets mots par format** (`identity.script_word_budget`, 145-165 wpm) :
  short 13 s -> 31-36 mots ; long 60 s -> 145-165 mots. Avant : 50-85 mots
  demandes meme pour le 13 s (= 19-32 s reel, jamais 13 s).
- **Hooks specifiques** : templates de repli + titres reecrits (chiffre,
  erreur nommee, resultat) - les hooks vagues perdent ~4x plus de viewers ;
  hook a l'ecran lisible en < 2 s (max 12 mots voix, 3-7 mots a l'ecran).
- **Checkpoints fixes (V3.1, 27/10/2026)** : `video.checkpoint_times_s =
  [2.0, 8.0]` + dernier tiers (payoff) - remplace `checkpoint_at_pct = 0.45`
  (les anciennes cles restent tolerees si `checkpoint_times_s` est absent).
  2 s = apres le hook, 8 s = premier beat, dernier tiers = payoff.
- **Taux de bouclage (V3.1, 27/10/2026)** : `analytics.loop_target_pct =
  100`, `analytics.loop_min_pct = 80`. `analytics.py` demande
  `metrics=views,uniqueViewers` et retombe sur `unavailable` (ou `pending`
  si video trop jeune) - **`uniqueViewers` n'est PAS une metrique documentee
  de l'Analytics API v2** (verifie sur developers.google.com/youtube/analytics/
  metrics le 27/10/2026 ; l'ancienne metrique `uniques` est depresiee depuis
  le 31/10/2016). Elle n'existe que dans Studio > mode avance. Si l'API
  l'accepte un jour, le verdict s'allume tout seul ; sinon loop_source =
  `unavailable` et aucune fausse alerte.
- **Kill switch (V3.1, 27/10/2026)** : `optimizer.kill_switch = true` :
  - 3 Shorts consecutifs < `kill_family_pct` (30 %) dans la meme famille
    de hook -> famille bannie `kill_ban_shorts` (20) Shorts ;
  - 5 Shorts consecutifs < `kill_niche_pct` (40 %) dans la meme niche ->
    pause de `kill_niche_shorts` (10) Shorts, alerte Gmail si configuree
    (bug corrige : l'alerte niche ne partait jamais - `__mod__` sans %s).
  Etat : `data/optimizer/kill_switch.json`.
- **Baseline (V3.1, 27/10/2026)** : `optimizer.baseline_size = 10` - les
  ajustements numeriques de l'optimizer sont SUSPENDUS jusqu'a 10 Shorts
  de reference (`data/optimizer/baseline.json`, ecriture atomique + lock).
  Ensuite `baseline_delta` compare les 10 Shorts suivants : delta moyen
  d'avg_view_pct et d'engagement dans le doc optimizer.
- **Patterns actifs (V3.1, 27/10/2026)** : `optimizer.active_min_pct =
  70`, `active_min_n = 3`, `active_shorts = 5` - un pattern (dimension
  hook_type/duration_format/hook_variant/title_has_number) > 70 % sur
  >= 3 Shorts est FORCE dans les 5 Shorts suivants (directive "FORCE
  pattern" dans les prompts). Teardown des succes, pas seulement des
  echecs. Etat : `data/optimizer/active_patterns.json`.
- **Taxonomie hook_type unifiee** : l'optimizer utilise desormais
  `identity.hook_family` (question/number/negation/statement), meme
  classification que le A/B.
