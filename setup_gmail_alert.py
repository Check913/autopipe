"""Configuration de l'alerte Gmail (AutoPipe).

Usage : double-cliquer sur setup_gmail_alert.bat.
Prerequis : la validation en 2 etapes est activee sur le compte Google, puis
un mot de passe d'application (16 caracteres) est cree sur:
    https://myaccount.google.com/apppasswords

Le script :
  1. demande l'adresse Gmail et le mot de passe d'application,
  2. envoie un email de test pour prouver que ca marche,
  3. pousse GMAIL_USER et GMAIL_APP_PASSWORD dans les secrets GitHub chiffres.
Le mot de passe ne transite par aucun fichier, chat ni historique git.
"""
import getpass
import sys

from reauth_youtube import push_github_secret


def smtp_test(user, pwd, to):
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(
        "L'alerte AutoPipe est bien configuree.\n\n"
        "Tu recevras un email ici :\n"
        "- si le token YouTube doit etre re-autorise (avec la marche a suivre,\n"
        "  2 minutes sur ton PC : double-cliquer reauth_247.bat),\n"
        "- si un run du bot echoue (avec le lien vers les logs).\n",
        "plain", "utf-8")
    msg["Subject"] = "[AutoPipe] Alerte Gmail configuree avec succes"
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)


def main():
    print("=== AutoPipe : configuration de l'alerte Gmail ===")
    print()
    print("Prerequis (une seule fois) :")
    print("  1. active la validation en 2 etapes de ton compte Google")
    print("  2. cree un mot de passe d'application (16 caracteres) :")
    print("     https://myaccount.google.com/apppasswords")
    print()
    user = input("Adresse Gmail (ex: prenom.nom@gmail.com) : ").strip()
    if "@" not in user or "." not in user.split("@")[1]:
        sys.exit("Adresse Gmail invalide.")
    pwd = getpass.getpass(
        "Mot de passe d'application (16 car., saisie invisible) : "
    ).strip().replace(" ", "")
    if len(pwd) != 16:
        sys.exit("Un mot de passe d'application Gmail fait 16 caracteres\n"
                 "(ce n'est PAS ton mot de passe Google habituel).")
    print()
    print("Test d'envoi vers %s ..." % user)
    try:
        smtp_test(user, pwd, user)
    except Exception as exc:
        sys.exit("Test SMTP echoue (%s)\nVerifie le mot de passe d'application "
                 "(regenere-le si besoin) puis reessaie." % exc)
    print("Email de test envoye ! Regarde ta boite (et les spams).")
    print()
    print("Enregistrement dans les secrets GitHub chiffres...")
    push_github_secret("GMAIL_USER", user)
    push_github_secret("GMAIL_APP_PASSWORD", pwd)
    print("Termine ! Les alertes AutoPipe arriveront desormais sur %s." % user)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nAnnule.")
        sys.exit(130)
    except Exception as exc:
        print("\nERREUR: %s" % exc)
        sys.exit(1)