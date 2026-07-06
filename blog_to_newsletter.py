#!/usr/bin/env python3
"""
blog_to_newsletter.py

Surveille les nouveaux articles de blog publiés sur une base Odoo (site web)
et crée automatiquement une campagne email correspondante dans Brevo.

Pensé pour être lancé périodiquement via cron / GitHub Actions (ex: toutes
les 15 minutes), sur le même principe que le script de notification Discord.

--------------------------------------------------------------------
CONFIGURATION - à adapter avant le premier lancement
--------------------------------------------------------------------
"""

import xmlrpc.client
import json
import os
import re
import sys
import logging
import requests
from datetime import datetime


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

# --- Brevo (destination des newsletters) ---
BREVO_API_KEY = _require_env("BREVO_API_KEY")
BREVO_LIST_ID = int(_require_env("BREVO_LIST_ID"))
BREVO_SENDER_EMAIL = _require_env("BREVO_SENDER_EMAIL")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "Martinews Webradio")

BREVO_API_URL = "https://api.brevo.com/v3/emailCampaigns"

AUTO_SEND = os.environ.get("AUTO_SEND", "false").strip().lower() == "true"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blog_to_newsletter.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


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


def fetch_new_blog_posts(site_conn, processed_ids):
    """Récupère les articles publiés qui ne sont pas encore dans processed_ids."""
    domain = [("is_published", "=", True)]
    if processed_ids:
        domain.append(("id", "not in", processed_ids))

    fields = ["id", "name", "subtitle", "teaser", "website_url", "create_date", "content", "cover_properties", "blog_id"]
    posts = site_conn.execute(
        "blog.post", "search_read", domain, fields, order="create_date asc"
    )
    return posts


def keep_only_latest_per_blog(posts):
    """Parmi une liste d'articles, ne garde que le plus récent pour chaque blog."""
    latest_by_blog = {}
    all_ids = []

    for post in posts:
        all_ids.append(post["id"])
        blog = post.get("blog_id")
        blog_key = blog[0] if isinstance(blog, (list, tuple)) else blog

        current_best = latest_by_blog.get(blog_key)
        if current_best is None or post["create_date"] > current_best["create_date"]:
            latest_by_blog[blog_key] = post

    posts_to_mail = list(latest_by_blog.values())
    return posts_to_mail, all_ids


def get_site_base_url(site_conn):
    try:
        param = site_conn.execute("ir.config_parameter", "get_param", "web.base.url")
        return param.rstrip("/") if param else SITE_URL
    except Exception:
        return SITE_URL


def extract_cover_image(post, base_url):
    cover_properties = post.get("cover_properties")
    if cover_properties:
        try:
            props = json.loads(cover_properties) if isinstance(cover_properties, str) else cover_properties
            bg = props.get("background-image", "")
            match = re.search(r"url\((.*?)\)", bg)
            if match:
                url = match.group(1).strip("'\"")
                if url.startswith("/"):
                    url = base_url + url
                return url
        except (json.JSONDecodeError, AttributeError):
            pass

    content = post.get("content") or ""
    match = re.search(r'<img[^>]+src="([^"]+)"', content)
    if match:
        url = match.group(1)
        if url.startswith("/"):
            url = base_url + url
        return url

    return None


def build_mailing_body(post, base_url):
    title = post.get("name") or "Nouvel article"
    subtitle = post.get("subtitle") or ""
    teaser = post.get("teaser") or ""
    link = f"{base_url}{post.get('website_url', '')}"
    image_url = extract_cover_image(post, base_url)

    ACCENT_COLOR = "#e63946"
    HEADER_BG = "#1d3557"
    TEXT_COLOR = "#333333"
    MUTED_COLOR = "#6c757d"
    BG_COLOR = "#f4f4f7"

    if image_url:
        header_block = f"""
        <tr><td style="padding:0;">
          <img src="{image_url}" alt="{title}" width="600"
               style="width:100%; max-width:600px; height:auto; display:block; border:0;" />
        </td></tr>
        """
    else:
        header_block = f"""
        <tr><td style="background-color:{HEADER_BG}; padding:40px 30px; text-align:center;">
          <span style="color:#ffffff; font-size:14px; letter-spacing:2px; text-transform:uppercase; opacity:0.8;">
            Nouvel article
          </span>
        </td></tr>
        """

    subtitle_html = (
        f'<p style="margin:8px 0 0 0; font-size:16px; color:{MUTED_COLOR}; font-weight:400;">{subtitle}</p>'
        if subtitle else ""
    )

    body = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background-color:{BG_COLOR}; padding:24px 0; font-family:'Helvetica Neue', Arial, sans-serif;">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0"
           style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
      {header_block}
      <tr><td style="padding:32px 30px 8px 30px;">
        <p style="margin:0; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; color:{ACCENT_COLOR}; font-weight:700;">
          Martinews Webradio
        </p>
        <h1 style="margin:10px 0 0 0; font-size:24px; line-height:1.3; color:{TEXT_COLOR};">{title}</h1>
        {subtitle_html}
      </td></tr>
      <tr><td style="padding:16px 30px 8px 30px;">
        <p style="margin:0; font-size:15px; line-height:1.6; color:{TEXT_COLOR};">{teaser}</p>
      </td></tr>
      <tr><td style="padding:24px 30px 32px 30px;" align="center">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td style="border-radius:6px; background-color:{ACCENT_COLOR};">
            <a href="{link}" style="display:inline-block; padding:14px 32px; font-size:15px; font-weight:600;
               color:#ffffff; text-decoration:none; border-radius:6px;">Lire l'article complet →</a>
          </td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:20px 30px; background-color:{BG_COLOR}; text-align:center; border-top:1px solid #eeeeee;">
        <p style="margin:0; font-size:12px; color:{MUTED_COLOR};">
          Vous recevez cet email car vous êtes inscrit à la newsletter de Martinews Webradio.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
"""
    return body


def create_mailing(post, base_url):
    """Crée (et envoie éventuellement) une campagne email Brevo pour un article donné."""
    subject = f"Nouvel article : {post.get('name')}"
    body_html = build_mailing_body(post, base_url)

    payload = {
        "name": subject,
        "subject": subject,
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "type": "classic",
        "htmlContent": body_html,
        "recipients": {"listIds": [BREVO_LIST_ID]},
    }
    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = requests.post(BREVO_API_URL, headers=headers, json=payload, timeout=30)
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Échec de création de la campagne Brevo (HTTP {response.status_code}) : {response.text}"
        )

    campaign_id = response.json().get("id")
    log.info("Campagne Brevo créée (id=%s) pour l'article '%s'.", campaign_id, post.get("name"))

    if AUTO_SEND:
        send_url = f"{BREVO_API_URL}/{campaign_id}/sendNow"
        send_response = requests.post(send_url, headers=headers, timeout=30)
        if send_response.status_code not in (200, 201, 202, 204):
            raise RuntimeError(
                f"Échec de l'envoi de la campagne Brevo {campaign_id} "
                f"(HTTP {send_response.status_code}) : {send_response.text}"
            )
        log.info("Campagne Brevo %s envoyée automatiquement.", campaign_id)
    else:
        log.info("Campagne Brevo %s laissée en BROUILLON.", campaign_id)

    return campaign_id


def main():
    log.info("=== Lancement blog_to_newsletter ===")
    state = load_state()
    processed_ids = state.get("processed_ids", [])
    is_first_run = len(processed_ids) == 0

    try:
        site_conn = OdooConnection(SITE_URL, SITE_DB, SITE_USER, SITE_API_KEY)
    except Exception as e:
        log.error("Connexion à la base Site Web impossible : %s", e)
        sys.exit(1)

    base_url = get_site_base_url(site_conn)

    try:
        candidate_posts = fetch_new_blog_posts(site_conn, processed_ids)
    except Exception as e:
        log.error("Erreur lors de la récupération des articles : %s", e)
        sys.exit(1)

    if not candidate_posts:
        log.info("Aucun nouvel article publié.")
        return

    if is_first_run:
        log.info("Premier lancement détecté (%d article(s)). Seul le dernier article de chaque blog sera envoyé.", len(candidate_posts))
        posts_to_mail, all_ids = keep_only_latest_per_blog(candidate_posts)

        for post in posts_to_mail:
            try:
                create_mailing(post, base_url)
            except Exception as e:
                log.error("Échec pour l'article id=%s ('%s') : %s", post.get("id"), post.get("name"), e)

        processed_ids.extend(all_ids)
        state["processed_ids"] = processed_ids
        save_state(state)
        log.info("Etat initialisé avec %d article(s) marqué(s) comme traités.", len(all_ids))

    else:
        log.info("%d nouvel(aux) article(s) détecté(s).", len(candidate_posts))
        for post in candidate_posts:
            try:
                create_mailing(post, base_url)
                processed_ids.append(post["id"])
                state["processed_ids"] = processed_ids
                save_state(state)
            except Exception as e:
                log.error("Échec pour l'article id=%s ('%s') : %s", post.get("id"), post.get("name"), e)

    log.info("=== Fin d'exécution ===")


if __name__ == "__main__":
    main()
