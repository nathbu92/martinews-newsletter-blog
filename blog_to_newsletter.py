#!/usr/bin/env python3
"""
blog_to_newsletter.py

Surveille les nouveaux articles de blog publiés sur une base Odoo (site web)
et crée automatiquement une newsletter correspondante dans une AUTRE base Odoo
(celle qui héberge l'app Email Marketing).

Pensé pour être lancé périodiquement via cron (ex: toutes les 15 minutes),
sur le même principe que ton script de notification Discord.

--------------------------------------------------------------------
CONFIGURATION - à adapter avant le premier lancement
--------------------------------------------------------------------
"""

import xmlrpc.client
import json
import os
import sys
import logging
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Toutes les valeurs sensibles sont lues depuis des variables d'environnement
# (voir README / secrets GitHub Actions). Ça évite de jamais écrire une clé
# API en clair dans ce fichier, même en local.
# ---------------------------------------------------------------------------

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Variable d'environnement manquante : {name}. "
            "Vérifie ta configuration (.env en local, ou Secrets sur GitHub Actions)."
        )
    return value

# --- Base "Site Web" (source des articles) ---
SITE_URL = _require_env("SITE_URL")
SITE_DB = _require_env("SITE_DB")
SITE_USER = _require_env("SITE_USER")
SITE_API_KEY = _require_env("SITE_API_KEY")

# --- Base "Email Marketing" (destination des newsletters) ---
MAIL_URL = _require_env("MAIL_URL")
MAIL_DB = _require_env("MAIL_DB")
MAIL_USER = _require_env("MAIL_USER")
MAIL_API_KEY = _require_env("MAIL_API_KEY")

# ID de la liste de diffusion cible (voir procédure B2)
MAILING_LIST_ID = int(os.environ.get("MAILING_LIST_ID", "2"))

# Nom "expéditeur" affiché dans les mails envoyés
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Martinews Webradio")

# Si "true" : le mailing est directement mis en file d'envoi.
# Si "false" (recommandé au début) : le mailing reste en brouillon.
AUTO_SEND = os.environ.get("AUTO_SEND", "false").strip().lower() == "true"

# Fichier qui garde la trace des articles déjà traités
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Fichier de log
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blog_to_newsletter.log")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ETAT (articles déjà traités)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Impossible de lire state.json (%s), on repart de zéro.", e)
    return {"processed_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CONNEXION ODOO (XML-RPC)
# ---------------------------------------------------------------------------

class OdooConnection:
    """Petit wrapper XML-RPC pour se connecter à une base Odoo avec une clé API."""

    def __init__(self, url, db, username, api_key):
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.api_key = api_key
        self.uid = None
        self.models = None
        self._connect()

    def _connect(self):
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(self.db, self.username, self.api_key, {})
        if not self.uid:
            raise RuntimeError(
                f"Authentification échouée sur {self.url} (vérifie DB / user / clé API)."
            )
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def execute(self, model, method, *args, **kwargs):
        return self.models.execute_kw(
            self.db, self.uid, self.api_key, model, method, list(args), kwargs
        )


# ---------------------------------------------------------------------------
# LOGIQUE METIER
# ---------------------------------------------------------------------------

def fetch_new_blog_posts(site_conn, processed_ids):
    """Récupère les articles publiés qui ne sont pas encore dans processed_ids."""
    domain = [("is_published", "=", True)]
    if processed_ids:
        domain.append(("id", "not in", processed_ids))

    fields = ["id", "name", "subtitle", "teaser", "website_url", "create_date"]
    posts = site_conn.execute(
        "blog.post", "search_read", domain, fields, order="create_date asc"
    )
    return posts


def get_site_base_url(site_conn):
    """Récupère l'URL publique du site pour construire des liens absolus."""
    try:
        param = site_conn.execute(
            "ir.config_parameter", "get_param", "web.base.url"
        )
        return param.rstrip("/") if param else SITE_URL
    except Exception:
        return SITE_URL


def build_mailing_body(post, base_url):
    title = post.get("name") or "Nouvel article"
    subtitle = post.get("subtitle") or ""
    teaser = post.get("teaser") or ""
    link = f"{base_url}{post.get('website_url', '')}"

    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto;">
        <h1 style="color: #333;">{title}</h1>
        {f'<h3 style="color:#666; font-weight:normal;">{subtitle}</h3>' if subtitle else ''}
        <p style="font-size: 15px; color: #444; line-height: 1.5;">{teaser}</p>
        <p>
            <a href="{link}"
               style="display:inline-block; padding:10px 20px; background:#714B67;
                      color:#fff; text-decoration:none; border-radius:4px;">
                Lire l'article complet
            </a>
        </p>
    </div>
    """
    return body


def create_mailing(mail_conn, post, base_url):
    subject = f"Nouvel article : {post.get('name')}"
    body_html = build_mailing_body(post, base_url)

    mailing_id = mail_conn.execute(
        "mailing.mailing",
        "create",
        {
            "subject": subject,
            "body_arch": body_html,
            "body_html": body_html,
            "mailing_model_id": mail_conn.execute(
                "ir.model", "search", [("model", "=", "mailing.list")]
            )[0],
            "contact_list_ids": [(6, 0, [MAILING_LIST_ID])],
            "email_from": f"{MAIL_FROM_NAME} <{MAIL_USER}>",
        },
    )
    log.info("Mailing créé (id=%s) pour l'article '%s'.", mailing_id, post.get("name"))

    if AUTO_SEND:
        mail_conn.execute("mailing.mailing", "action_send_mail", [mailing_id])
        log.info("Mailing %s envoyé automatiquement.", mailing_id)
    else:
        log.info(
            "Mailing %s laissé en BROUILLON — va le valider manuellement dans Odoo.",
            mailing_id,
        )

    return mailing_id


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=== Lancement blog_to_newsletter ===")
    state = load_state()
    processed_ids = state.get("processed_ids", [])

    try:
        site_conn = OdooConnection(SITE_URL, SITE_DB, SITE_USER, SITE_API_KEY)
    except Exception as e:
        log.error("Connexion à la base Site Web impossible : %s", e)
        sys.exit(1)

    try:
        mail_conn = OdooConnection(MAIL_URL, MAIL_DB, MAIL_USER, MAIL_API_KEY)
    except Exception as e:
        log.error("Connexion à la base Email Marketing impossible : %s", e)
        sys.exit(1)

    base_url = get_site_base_url(site_conn)

    try:
        new_posts = fetch_new_blog_posts(site_conn, processed_ids)
    except Exception as e:
        log.error("Erreur lors de la récupération des articles : %s", e)
        sys.exit(1)

    if not new_posts:
        log.info("Aucun nouvel article publié.")
        return

    log.info("%d nouvel(aux) article(s) détecté(s).", len(new_posts))

    for post in new_posts:
        try:
            create_mailing(mail_conn, post, base_url)
            processed_ids.append(post["id"])
            state["processed_ids"] = processed_ids
            save_state(state)  # on sauvegarde après CHAQUE article traité
        except Exception as e:
            log.error(
                "Échec de création du mailing pour l'article id=%s ('%s') : %s",
                post.get("id"), post.get("name"), e,
            )
            # on ne marque pas comme traité -> il sera retenté au prochain passage

    log.info("=== Fin d'exécution ===")


if __name__ == "__main__":
    main()
