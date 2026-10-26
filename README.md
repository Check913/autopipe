# AutoPipe

Pipeline YouTube automatise : tendances -> idees/scripts -> voix -> sous-titres -> clips -> montage -> upload -> stats.

## Prerequis

- Windows 10/11 + Python 3.10+
- ffmpeg dans le PATH : `winget install Gyan.FFmpeg` (ou `choco install ffmpeg`)
- Connexion internet

## Installation

```
pip install -r requirements.txt
python -m autopipe.cli setup
copy config.example.toml autopipe.toml
```

Adapte ensuite `autopipe.toml` (niche, voix, cles).

## Cles et comptes

### Clips video : Pixabay (recommande) ou Pexels

#### Pixabay - cle gratuite et immediate (conseillee)
1. Cree un compte gratuit sur https://pixabay.com/ puis ouvre https://pixabay.com/api/docs/
   une fois connecte : ta cle API personnelle apparait sous "Your API key" (utilisable immediatement).
2. Colle-la dans `autopipe.toml` -> `fetch_media.pixabay_api_key`
   (ou variable d'environnement `PIXABAY_API_KEY`)
3. `fetch_media.provider = "auto"` : AutoPipe choisit Pixabay des qu'une cle Pixabay est
   presente (sinon Pexels). Tu peux aussi forcer `provider = "pixabay"` ou `provider = "pexels"`.
- Attribution non requise, rendus verticaux disponibles, gratuit (limites de debit : voir la doc API).
- NB (oct. 2026) : Pexels a mis en pause la creation de nouvelles cles API -> Pixabay est la voie recommandee.

#### Pexels (alternative)
1. Cle gratuite sur https://www.pexels.com/api/
2. Colle-la dans `autopipe.toml` -> `fetch_media.pexels_api_key`
   (ou variable d'environnement `PEXELS_API_KEY`)

### YouTube (upload)
1. https://console.cloud.google.com -> cree un projet
2. Active "YouTube Data API v3"
3. Ecran de consentement OAuth : type "Externe", ajoute ton Gmail comme testeur
4. Credentiels -> "Cree des identifiants" -> "ID client OAuth" -> "Application de bureau"
5. Telecharge le JSON -> renomme-le `client_secret.json` a la racine du projet
6. Premier upload : une fenetre navigateur s'ouvre (connecte-toi avec le compte de la chaine) -> le token est sauvegarde dans `token.json`

Quota API par defaut : 10 000 unites/jour, 1 upload ≈ 1600 unites -> ~6 videos/jour
(d'ou `upload.daily_limit = 6`).

### LLM (optionnel mais conseille)
Avec une API compatible OpenAI (`llm.base_url` + `llm.api_key` dans `autopipe.toml`),
les scripts sont generes par le LLM -> textes plus originaux. Sans LLM, AutoPipe
utilise des templates bases sur les tendances.

## Utilisation

```
python -m autopipe.cli setup         # verifier l'installation
python -m autopipe.cli test-tts      # test de la voix
python -m autopipe.cli test-video    # test montage complet (sans cle Pexels)
python -m autopipe.cli scout         # tendances du jour
python -m autopipe.cli ideas -n 5    # 5 idees + scripts sauvegardes
python -m autopipe.cli remake -n 2   # Super-Remake : remakes de Shorts viraux
python -m autopipe.cli run           # pipeline complet : 1 video
python -m autopipe.cli run --no-upload
python -m autopipe.cli serve         # boucle 24/7 : 1 video / 4 h (Ctrl+C pour arreter)
python -m autopipe.cli upload out.mp4 --title "Titre" --tags mot1 mot2
python -m autopipe.cli analytics     # stats des dernieres videos
```

Mode 24/7 sans fenetre : double-clic sur `start.bat` (ou planifie avec le
Planificateur de taches Windows au demarrage).

## Conseils monetisation (a lire)

- Depuis le 15/07/2025, YouTube demonetise le contenu "mass-produced/repetitive".
  Les scripts AutoPipe partent des tendances, mais ajoute ta valeur (faits verifies,
  angle original) : c'est ce qui differencie une chaine monetisee d'une chaine bannie.
- Seuils YPP : 1 000 abonnes + 4 000 h de watch (ou 10 M de vues Shorts).
- CTR vise 4-8 %, retention des 30 premieres secondes critique : les premiers plans
  video (hook) sont la cle.
- Commence en `privacy = "unlisted"` pour valider le rendu avant de publier.

---

## Edition USA (audience anglophone)

Cette version est configuree par defaut pour une audience US :

- `general.language = "en"`, `general.region = "US"` : tendances US + mots-cles anglais
- Voix TTS : `en-US-AndrewNeural` (alternatives : `en-US-ChristopherNeural`, `en-US-EricNeural`, `en-US-AriaNeural`)
- Requetes de clips en anglais (`nature`, `city`, `technology`) via Pixabay ou Pexels
- Metadonnees YouTube : `defaultLanguage` et `defaultAudioLanguage` = `en` sur chaque upload
- Fenetres de publication : `[upload] post_hours_utc = [15, 21]` (15:00 UTC = 11h ET, 21:00 UTC = 17h ET).
  Le scheduler attend ces fenetres avant d'uploader. Vide ou supprime cette cle pour revenir au mode intervalle simple.

### Monetisation depuis la Cote d'Ivoire (important)

- La Cote d'Ivoire n'est PAS sur la liste officielle du Programme Partenaire YouTube (YPP) (verifie en 2026).
  AdSense est disponible en CI (virement bancaire FCFA), mais l'inscription YPP reste bloquee localement.
- N'utilise PAS de VPN pour simuler une position US : Google peut detecter l'incoherence (verification
  telephone/PIN, adresse de paiement) et le compte risque la suspension - ce risque touche tout ce que tu construis.
- La voie legitime utilisee par beaucoup de createurs africains : creer une LLC americaine (ex. Wyoming
  ~100-150 USD + agent enregistre, ou Delaware), obtenir un EIN gratuit en ligne (IRS), ouvrir un compte
  pro US avec Payoneer ou Wise, puis connecter YPP + AdSense aux parametres de la LLC. La LLC declare
  aux USA, toi tu declares en CI.
- Fais valider ta situation fiscale (CI + LLC US) par un comptable avant le premier paiement.

### US mode - known behavior (2026-10-21)
- YouTube retired its Trending page: the URL /feed/trending now redirects to the home page and yt-dlp fails with "The channel/playlist does not exist". scout.py catches this, logs a warning, and falls back to keyword searches (search_keywords), which remain the primary US topic source.

## Visibilite 2026 (actif par defaut)

4 optimisations integrees pour l'algorithme YouTube Shorts :

1. **Titres curiosity-gap** (`ideas.py`) : generes par LLM few-shot, sinon
   templates locaux (`TITLE_PATTERNS`) -> CTR plus eleve. Chaque idee contient
   aussi un champ `hook` (max 60 caracteres).
2. **Hook visuel 0-3s** (`subs.write_hook_ass` + `video.py`) : le hook est grave
   en gros texte jaune contour noir en haut de l'ecran pendant les 3 premieres
   secondes -> reduit le swipe-away au moment critique. Config `[video]` :
   hook_overlay / hook_duration / hook_font_size.
3. **Fin en boucle** (`video.build_video_track`) : les dernieres images de la
   video reprennent les toutes premieres -> boucle visuelle infinie, retention
   et replays en hausse. Config `[video]` : loop_ending / loop_tail.
4. **Commentaire automatique** (`upload.post_pinned_comment`) : un commentaire
   engageant est publie sous chaque video juste apres l'upload (le commentaire
   du proprietaire apparait en tete ; epinglage manuel en 1 clic dans YouTube
   Studio si voulu). Config `[upload]` : auto_comment / comment_template
   (variables disponibles : {title}, {hook}, {tags}). Necessite de re-valider
   OAuth une fois (nouveau scope youtube.force-ssl ajoute).

Publication US : fenetres 15h et 21h UTC (11h/17h ET) via `post_hours_utc`
(tolerance 5 minutes). Meilleurs creneaux Shorts US : vendredi 16h/18h/19h.

## Super-Remake "10% Better" (2026-10-26)

Nouveau mode de creation : le bot ne part plus seulement des tendances, il
part d'un Short qui a DEJA fait ses preuves dans la niche.

Pipeline (`autopipe/remake.py`) :
1. **Scout viral** : yt-dlp scrape les Shorts les plus vus des mots-cles de la
   niche (filtre `remake.min_views` = 500 000 vues, `max_duration` = 75 s,
   hydratation des vues si la recherche flat ne les expose pas).
2. **Analyse structurelle** : les sous-titres du hit sont recuperes et le LLM
   decode POURQUOI il a marche -> Core Hook (pourquoi on ne swipe pas dans les
   3 premieres secondes), formule psychologique (curiosity gap, shock, open
   loop, stakes, payoff), rythme (wpm, style de phrase, duree du hook), sujet.
3. **Regeneration "10% Better"** : le moteur reecrit un Short TOTALEMENT
   original qui applique la MEME formule avec une execution plus forte :
   ouverture choc, phrases ultra-courtes, faits mieux documentes, fin en
   boucle, et un upgrade que l'original n'avait pas.
4. **Garde anti-duplication** : similarite Jaccard titre + script vs
   l'original ; au-dessus de `remake.max_jaccard` (0.35) le remake est REJETE,
   jamais sauvegarde ni publie.

- Fichiers : `data/remake/hits_latest.json` (hits scannes),
  `data/remake/remake_*.json` (remakes valides), `remakes_latest.json`.
- Anti-doublon : un Short deja remake n'est jamais remaker une 2e fois.
- Integration scheduler : si `[remake] enabled = true` (actif dans
  `autopipe.toml`), `autopipe serve` / `autopipe run` remakent d'abord ; tout
  echec (reseau, LLM, aucun hit valide) retombe AUTOMATIQUEMENT sur les idees
  standards -> le pipeline ne bloque jamais.
- LLM tres conseille ici : sans LLM, le remake retombe sur une variante
  template (generator `remake:fallback`) moins puissante.
- Legal/YouTube : on copie la FORMULE virale (structure psychologique), jamais
  le contenu : aucun texte n'est reutilise (garde Jaccard + interdiction dans
  le prompt). La video source n'est ni telechargee ni re-uploadee.

## Depannage reseau / proxy (2026-10-23)

Symptome observe dans le log du 2026-10-23 (20:42-21:05) : tout echouait avec

```
requests.exceptions.ProxyError: ... SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number'))
```

Cause : un proxy etait declare avec un mauvais schema (schema `https://` vers
un port local qui parle HTTP pur), a DEUX endroits simultanement :
1. Variables d'environnement (`HTTPS_PROXY=https://127.0.0.1:59832`).
2. REGISTRE WINDOWS `HKCU\...\Internet Settings` : `ProxyEnable=1`,
   `ProxyServer=http=127.0.0.1:59832;https=127.0.0.1:59832;socks=127.0.0.1:59831`,
   `ProxyOverride=<local>`. Python (urllib/requests) lit ce registre meme sans
   variable d'environnement -> le piege restait actif apres avoir purge les env.

Dans les deux cas urllib3 tentait une poignee de main TLS vers un proxy HTTP
-> `WRONG_VERSION_NUMBER`, ce qui cassait Gemini, Pixabay, OAuth
(oauth2.googleapis.com) et l'upload YouTube (httplib2).

AutoPipe neutralise desormais ce piege au demarrage (`util.fix_proxy_env`,
appele par `cli._init` et `scheduler.run_pipeline_once`) : en mode `direct`
les variables ET le registre sont ignores (verifie le 2026-10-23 :
`urllib.getproxies()` renvoie `{'no': '*'}` et `GET
https://www.googleapis.com/oauth2/v3/certs` repond 200 avec le registre
empoisonne). Reglage dans
`autopipe.toml` :

```
[network]
proxy_mode = "direct"   # ignore les variables proxy (recommande ici)
# proxy_mode = "fix"    # reecrit https:// en http:// pour un proxy local mal declare
# proxy_mode = "keep"   # ne modifie rien
```

Autres correctifs du 2026-10-23 :
- `scheduler` : apres un pipeline echoue, attente de 5 minutes (plus de boucle
  de relances immediates en serie).
- `tts.synthesize` : 5 tentatives, backoff exponentiel 2/4/8/16 s + jitter
  aleatoire et ROTATION DE VOIX de secours (Christopher/Aria/Eric, cle
  `[tts] fallback_voices`). Le `NoAudioReceived` de edge-tts est transient
  cote Microsoft (3 echecs rapides a 22:29, succes 15 min plus tard ; le
  service est deja tombe 1-2 min a 21:00). Le suivi de bouche capture
  desormais `WordBoundary` ET `SentenceBoundary` (toutes versions d'edge-tts).
- `fetch_media` : requetes de clips nettoyees (titres bruts et tag `shorts`
  supprimes) -> resultats Pixabay bien plus pertinents.
- `subs._ass_escape` : fin du crash `unterminated character set` -> l'overlay
  hook 0-3s revient sur les videos.
- `ideas` / `llm` : double probleme Gemini diagnostique le 2026-10-23 :
  1) `gemini-2.0-flash` (et toute la serie 2.5 pour les nouveaux comptes) a ete
     RETIRE par Google -> 404. La cle, elle, est VALIDE (Bearer accepte).
  2) Tous les modeles actuels (Gemini 3.x, Gemma 4, endpoint natif ET OpenAI-
     compatible) repondent `403 "Your project has been denied access. Please
     contact support."` : le projet Google de la cle est BLOQUE cote Google.
  Actions : creer une nouvelle cle sur https://aistudio.google.com/apikey (ou
  contacter le support Google pour le projet) et la coller dans
  `autopipe.toml` -> `llm.api_key`. Le modele est deja regle sur
  `gemini-3.8-flash` (remplacement recommande par le message d'erreur Google).
  En attendant, AutoPipe continue de fonctionner avec les templates locaux
  (fallback automatique). La liste des modeles disponibles s'obtient via
  `GET https://generativelanguage.googleapis.com/v1beta/openai/models`, et le
  log LLM affiche desormais le message complet du fournisseur (plus de simple
  "404 Client Error" illisible).
- `upload` : durcissement reseau et scopes (2026-10-23) :
  1) httplib2 verrouille en direct (`Http(timeout=120,
     proxy_info=lambda *a, **k: None)`) : la bibli ne consulte ni variables
     ni registre proxy (verifie : discovery YouTube 200 avec registre
     empoisonne). `proxy_mode` est lu dans `[network]` d'`autopipe.toml`.
  2) Verification des scopes OAuth avant upload : si `youtube.force-ssl`
     manque dans `token.json`, re-autorisation automatique (fenetre
     navigateur) au lieu d'un 403 illisible cote API.
  3) Message explicite si `client_secret.json` est absent (fin du crash
     `FileNotFoundError` brut).
- `cli video` : la commande lisait `pexels_clips.json` (ancien nom) alors que
  `fetch_media` ecrit `media_clips.json` -> crash au premier run apres le
  pipeline. Desormais `media_clips.json` (repli legacy `pexels_clips.json`).
  Verifie le 2026-10-23 : reconstruction de la video du run
  `20261023-224559-01` depuis son fichier idee, exit 0, mp4 22,9 s.
- IMPORTANT : au prochain upload, Google demandera une RE-AUTORISATION (le
  code OAuth du 21:05 n'a pas ete consomme a cause du proxy). Une fenetre
  navigateur s'ouvrira : connecte-toi et accepte, le nouveau token sera
  sauvegarde dans `token.json`.
## Audit zero vues + contenu reel (2026-10-24)

Diagnostic de la video `KoHNX5qZD2c` (publiee le 23/10, 2 vues en ~8 h) :
c'est NORMAL pour un canal tout neuf (distribution seed tres faible), MAIS la
vraie cause etait le contenu : le fallback `ideas` generait un script 100%
template SANS aucune information ("First: the basics that actually matter..."),
et 3 runs d'affilee ont produit le MEME sujet ("String Theory...") car aucune
deduplication inter-runs. Depuis le 1er octobre 2026, YouTube demote
explicitement le contenu generique/re-uploade et favorise l'original.

Comment l'algorithme Shorts fonctionne (verifie le 2026-10-24, sources 2026) :
YouTube montre chaque Short a une petite audience test, puis lit si les
viewers swipe-away dans les 1-3 premieres secondes (premier filtre), le taux
de visionnage complet (cible 70 %+), les re-visionnages/boucles, les partages.
Si le test passe, distribution elargie par paliers ; sinon distribution stoppee.
Un canal a 0 abonnes part sur la meme base qu'un gros canal : le contenu seul
decide. Aucun parametre technique du bot ne peut FORCER des vues (les bots de
vues/abonnes mènent a la suppression du canal). Ce qui augmente les chances :
un vrai contenu, un hook < 3 s, des sujets non dupliques, regularite.

Correctifs appliques dans `autopipe/ideas.py` (verifies le 2026-10-24) :
1. Fallback AVEC contenu reel : `fallback_idea` recupere 2-3 phrases reelles
   depuis l'API resume Wikipedia (gratuit, sans cle ; nettoyage des titres
   YouTube clibateux type "X? (Explained in 5 Minutes)" puis recherche de
   secours). Generator `fallback+wiki`. Repli template pur si Wikipedia
   indisponible. Teste : sujet "String theory" -> script factuel complet.
2. Deduplication inter-runs : `_used_topics` lit tous les `data/ideas/*.json`
   (source_topic + titre, normalises) et `run_ideas` ecarte les sujets deja
   traites (demande 4x plus de candidats au scout). Verifie : le run du
   24/10 a produit "The Science of Rainbows" et non un doublon.
3. Titres : `_curiosity_title` coupe desormais sur un MOT ENTIER (jamais
   "Explained – What is The" tronque en plein mot).
- Pipeline complet re-verifie apres correctifs : `run --no-upload` exit 0,
  idea `20261024-080706-01` (fallback+wiki), TTS 77 mots, 6 clips Pixabay,
  video 1080x1920 construite.
- Rappel LLM : la cle Gemini actuelle reste BLOQUEE par Google. Contre-verify
  le 2026-10-24 : la cle authentifie (liste des modeles -> 200) mais TOUTE
  generation renvoie 403 "Your project has been denied access" sur TOUS les
  modeles testes (gemini-3.8-flash inexistant, gemini-flash-latest,
  gemini-3.1-flash-lite, gemma-4-31b-it, gemma-4-26b-a4b-it via endpoint
  OpenAI-compatible ; gemini-3.1-flash-lite via endpoint natif ; gemini-2.5-flash
  -> 404 retire pour les nouveaux comptes). Le blocage est donc au niveau du
  PROJET Google auquel la cle appartient, pas de la cle ni du code. Solution :
  creer une cle dans un NOUVEAU projet sur https://aistudio.google.com/apikey
  (ou avec un autre compte Google) et la coller dans autopipe.toml [llm].api_key
  ; aucun redemarrage necessaire (lu a chaque run). Tant qu'elle n'est pas
  remplacee, le bot tourne sur fallback+wiki (correct aujourd'hui, mais un LLM
  actif ecrirait des scripts bien plus accrocheurs).
## 2026-10-24 (apres-midi) : LLM REACTIVE — nouvelle cle Gemini operative

- Nouvelle cle Gemini fournie par l'utilisateur (projet VALIDE cette fois) : liste
  des modeles -> 200 (60 modeles), generation reelle -> 200 sur
  `gemini-2.5-flash`. Configuration `[llm]` mise a jour dans `autopipe.toml`
  (api_key + model = `gemini-2.5-flash`).
- Choix du modele (tests du 2026-10-24 08:30 UTC) :
  - `gemini-3.8-flash` : 503 "high demand" (temporaire, reessayer plus tard) ;
  - `gemini-flash-latest` : > 90 s sans reponse (timeout) ;
  - `gemini-2.5-flash` : 200 en quelques secondes — retenu.
- Correctif `autopipe/util.py` (LLMClient.chat) : `max_tokens` 1600 -> 4000.
  Cause racine : en mode `response_format=json_object`, gemini-2.5-flash
  "reflechit" avant de repondre et ces jetons de reflexion comptent dans
  `max_tokens` -> reponse JSON tronquee (coupee a 1088 chars en plein champ
  "description"). Avec 4000, JSON complet.
- Preuve end-to-end (script temporaire, supprime depuis) : `llm_idea()` via la
  config reelle -> `generator: "llm:gemini-2.5-flash"`, script 111 mots avec
  faits reels (rayonnement Hawking, 10^67 ans, Sagittarius A*), hook choc,
  fin en boucle, 12 tags, question de commentaire. Plus aucun template vide.
- Sync `AutoPipe1` (ex-AutoPipe2) : `util.py` + `autopipe.toml` copies a
  l'identique (les dossiers sont identiques, cf. diff).
- Serve relance (PID 25084, demarrage 08:35:44) : il a recharge la config avec
  la nouvelle cle. Prochaine fenetre de publication : 15:00 UTC — la video sera
  generee par le LLM.