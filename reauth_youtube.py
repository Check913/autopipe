"""Re-autorisation YouTube en un clic (AutoPipe).

Usage : double-cliquer sur reauth_247.bat (Windows).
Le script :
  1. connecte le service YouTube (ouvre le navigateur pour l'autorisation
     Google si le token local est expire/invalide),
  2. met a jour token.json,
  3. pousse le token dans le secret GitHub chiffre YOUTUBE_TOKEN_JSON,
  4. le bot 24/7 repart automatiquement au prochain run GitHub Actions.
"""
import base64
import os
import sys
from pathlib import Path

REPO = "Check913/autopipe"
GH_TOKEN_FILE = Path.home() / ".autopipe_gh_token"


def _ensure_pynacl():
    try:
        import nacl  # noqa: F401
    except ImportError:
        import subprocess
        print("Installation de pynacl (une seule fois)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--quiet", "pynacl"])


def gh_headers():
    if not GH_TOKEN_FILE.exists():
        sys.exit("Token GitHub introuvable: %s" % GH_TOKEN_FILE)
    tok = GH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not tok:
        sys.exit("Fichier vide: %s" % GH_TOKEN_FILE)
    return {"Authorization": "Bearer %s" % tok,
            "Accept": "application/vnd.github+json"}


def push_github_secret(name, value):
    """Chiffre la valeur (sealed box PyNaCl) et la pousse en secret du depot."""
    import requests
    from nacl.encoding import Base64Encoder
    from nacl.public import PublicKey, SealedBox

    # Comme fix_proxy_env("direct") : le proxy systeme Windows ne sait pas
    # tunneler le HTTPS (WRONG_VERSION_NUMBER) -> force la connexion directe.
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    headers = gh_headers()
    api = "https://api.github.com/repos/%s/actions/secrets" % REPO
    r = requests.get(api + "/public-key", headers=headers, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError("GitHub (public-key) %s: %s" % (r.status_code, r.text[:200]))
    data = r.json()
    key = PublicKey(data["key"].encode("utf-8"), Base64Encoder())
    encrypted = SealedBox(key).encrypt(value.encode("utf-8"))
    r = requests.put(api + "/" + name, headers=headers,
                     json={"encrypted_value":
                           base64.b64encode(encrypted).decode("ascii"),
                           "key_id": data["key_id"]},
                     timeout=30)
    if r.status_code >= 300:
        raise RuntimeError("GitHub (set %s) %s: %s"
                           % (name, r.status_code, r.text[:200]))


def main():
    _ensure_pynacl()
    from autopipe.config import get_paths, load_config
    from autopipe.util import fix_proxy_env, setup_logging
    cfg = load_config()
    setup_logging("INFO", cfg["general"].get("log_file"))
    fix_proxy_env(cfg.get("network", {}).get("proxy_mode", "direct"))

    from autopipe import upload
    print("[1/3] Connexion YouTube...")
    print("      (une fenetre de navigateur s'ouvre si une re-autorisation est")
    print("       necessaire: choisis le compte de la chaine puis clique")
    print("       \"Autoriser\". Sinon rien a faire, ca passe tout seul.)")
    svc = upload.get_service(cfg)
    me = svc.channels().list(part="snippet,statistics", mine=True).execute()
    items = me.get("items") or []
    if items:
        print("      Chaine OK: %s" % items[0]["snippet"].get("title", "?"))
    else:
        print("      Attention: aucune chaine visible pour ce compte.")

    paths = get_paths(cfg)
    token_file = paths.root / cfg["upload"].get("token_file", "token.json")
    if not token_file.exists():
        raise RuntimeError("token.json introuvable apres l'autorisation: %s"
                           % token_file)
    print("[2/3] Envoi du token vers GitHub (secret YOUTUBE_TOKEN_JSON)...")
    push_github_secret("YOUTUBE_TOKEN_JSON", token_file.read_text(encoding="utf-8"))
    print("[3/3] Termine ! Le bot 24/7 repartira tout seul au prochain run.")
    print("      Suivi: https://github.com/%s/actions" % REPO)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print("\nERREUR: %s" % exc)
        print("Reessaie en double-cliquant a nouveau sur reauth_247.bat.")
        sys.exit(1)