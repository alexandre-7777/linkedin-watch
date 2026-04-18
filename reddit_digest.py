#!/usr/bin/env python3
"""Weekly Reddit editorial digest — fetches top posts, summarizes with Claude, sends via Gmail."""

import os
import time
import json
import smtplib
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic

SUBREDDITS = [
    "Wordpress",
    "ClaudeAI",
    "airtable",
    "Infomaniak",
    "neurodiversity",
    "HighFunctioning",
]

MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
MIN_SCORE = 20
MIN_COMMENTS = 10
EMAIL_TO = "alexandre@web-ia.com"
USER_AGENT = "RedditDigestBot/1.0 by alexandre@web-ia.com"


def fetch_subreddit(subreddit: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit=25"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [p["data"] for p in data["data"]["children"]]


def filter_posts(posts: list[dict]) -> list[dict]:
    now = time.time()
    result = []
    for p in posts:
        age = now - p.get("created_utc", 0)
        if age > MAX_AGE_SECONDS:
            continue
        if p.get("score", 0) < MIN_SCORE and p.get("num_comments", 0) < MIN_COMMENTS:
            continue
        result.append(p)
    return result


def build_posts_text(subreddit: str, posts: list[dict]) -> str:
    lines = [f"Subreddit: r/{subreddit}"]
    for p in posts[:5]:
        lines.append(
            f"- [{p['title']}](https://reddit.com{p['permalink']}) "
            f"| score: {p['score']} | commentaires: {p['num_comments']}"
        )
        if p.get("selftext"):
            lines.append(f"  Extrait: {p['selftext'][:200]}")
    return "\n".join(lines)


def generate_digest(subreddit_posts: dict[str, list[dict]]) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    monday = datetime.now() - timedelta(days=datetime.now().weekday())
    months_fr = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    date_fr = f"{monday.day} {months_fr[monday.month - 1]} {monday.year}"

    content_blocks = []
    for sub, posts in subreddit_posts.items():
        if posts:
            content_blocks.append(build_posts_text(sub, posts))

    if not content_blocks:
        return ""

    prompt = f"""Tu es un rédacteur de revue de presse tech pour Alexandre.
Voici les posts Reddit notables de la semaine (score > {MIN_SCORE} ou > {MIN_COMMENTS} commentaires, moins de 7 jours) :

{chr(10).join(content_blocks)}

Rédige une revue de presse hebdomadaire en français :
- Objet: Revue Reddit - semaine du {date_fr}
- Format email (texte enrichi)
- Commence par "Bonjour Alexandre,"
- Une section par subreddit avec du contenu notable
- Pour chaque post retenu : titre cliquable, 2-3 phrases éditoriales (sujet, pourquoi notable, réaction communauté)
- Ignore les subreddits sans posts qualifiants
- Termine par "Prochain digest : vendredi prochain à 7h."
- Sois sélectif et éditorial : 3 excellents items valent mieux que 15 médiocres
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def send_email(subject: str, body: str):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, EMAIL_TO, msg.as_string())
    print(f"Email envoyé à {EMAIL_TO}")


def main():
    print("Démarrage de la revue Reddit hebdomadaire...")
    subreddit_posts: dict[str, list[dict]] = {}

    for sub in SUBREDDITS:
        print(f"  Fetching r/{sub}...")
        try:
            posts = fetch_subreddit(sub)
            filtered = filter_posts(posts)
            subreddit_posts[sub] = filtered
            print(f"    {len(filtered)} posts qualifiants sur {len(posts)}")
        except Exception as e:
            print(f"    Erreur r/{sub}: {e}")
            subreddit_posts[sub] = []

    total = sum(len(p) for p in subreddit_posts.values())
    if total == 0:
        print("Aucun post qualifiant cette semaine. Email non envoyé.")
        return

    print("Génération du digest avec Claude...")
    digest = generate_digest(subreddit_posts)

    if not digest:
        print("Digest vide. Email non envoyé.")
        return

    monday = datetime.now() - timedelta(days=datetime.now().weekday())
    months_fr = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    date_fr = f"{monday.day} {months_fr[monday.month - 1]} {monday.year}"
    subject = f"Revue Reddit - semaine du {date_fr}"

    print(f"Envoi de l'email : {subject}")
    send_email(subject, digest)
    print("Terminé.")


if __name__ == "__main__":
    main()
