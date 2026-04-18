#!/usr/bin/env python3
"""Weekly Reddit editorial digest — fetches top posts via RSS, summarizes with Claude, posts to Slack."""

import os
import re
import time
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import anthropic

# (subreddit, thème en français pour le digest)
SUBREDDITS = [
    ("Wordpress", "WordPress"),
    ("ClaudeAI", "Claude AI"),
    ("airtable", "Airtable"),
    ("Infomaniak", "Infomaniak"),
    ("neurodiversity", "Neuro-atypisme"),
    ("Giftedness", "HPI"),
]

MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_subreddit_rss(subreddit: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/top.rss?t=week&limit=25"
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read()

    root = ET.fromstring(content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    now = time.time()
    posts = []

    for entry in root.findall("atom:entry", ns):
        published = entry.findtext("atom:published", default="", namespaces=ns)
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            age = now - dt.timestamp()
        except ValueError:
            age = 0

        if age > MAX_AGE_SECONDS:
            continue

        title = entry.findtext("atom:title", default="", namespaces=ns)
        link_el = entry.find("atom:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        content_el = entry.find("atom:content", ns)
        body = content_el.text or "" if content_el is not None else ""
        body_text = re.sub(r"<[^>]+>", " ", body)[:300].strip()

        posts.append({
            "title": title,
            "url": link,
            "published": published,
            "preview": body_text,
        })

    return posts


def build_posts_text(subreddit: str, theme_fr: str, posts: list[dict]) -> str:
    lines = [f"Thème : {theme_fr} (r/{subreddit})"]
    for p in posts[:8]:
        lines.append(f"- [{p['title']}]({p['url']}) — publié le {p['published'][:10]}")
        if p["preview"]:
            lines.append(f"  Aperçu: {p['preview'][:200]}")
    return "\n".join(lines)


def generate_digest(subreddit_posts: dict[str, tuple[str, list[dict]]], date_fr: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content_blocks = [
        build_posts_text(sub, theme, posts)
        for sub, (theme, posts) in subreddit_posts.items()
        if posts
    ]

    prompt = f"""Tu es un rédacteur de revue de presse tech pour Alexandre.
Voici les posts Reddit de la semaine (moins de 7 jours), organisés par thème :

{chr(10).join(content_blocks)}

Rédige une revue de presse hebdomadaire en français pour Slack :
- Commence par : *Revue Reddit — semaine du {date_fr}*
- Une section par thème avec du contenu notable (utilise le nom du thème en français comme titre de section)
- Pour chaque post retenu : titre avec lien cliquable, 2-3 phrases éditoriales (sujet, pourquoi notable, ce que ça apporte)
- Ignore les thèmes sans posts intéressants cette semaine
- Termine par : _Prochain digest : vendredi prochain à 7h._
- Sois sélectif et éditorial : 3 excellents items valent mieux que 15 médiocres
- Utilise le format Slack (*gras*, _italique_, liens)
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def post_to_slack(text: str):
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"Slack response: {resp.status}")


def main():
    print("Démarrage de la revue Reddit hebdomadaire (via RSS)...")
    subreddit_posts: dict[str, tuple[str, list[dict]]] = {}

    for sub, theme in SUBREDDITS:
        print(f"  Fetching r/{sub} ({theme})...")
        try:
            posts = fetch_subreddit_rss(sub)
            subreddit_posts[sub] = (theme, posts)
            print(f"    {len(posts)} posts récents trouvés")
        except Exception as e:
            print(f"    Erreur r/{sub}: {e}")
            subreddit_posts[sub] = (theme, [])

    total = sum(len(p) for _, p in subreddit_posts.values())
    if total == 0:
        print("Aucun post récent. Message non envoyé.")
        return

    monday = datetime.now() - timedelta(days=datetime.now().weekday())
    months_fr = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    date_fr = f"{monday.day} {months_fr[monday.month - 1]} {monday.year}"

    print("Génération du digest avec Claude...")
    digest = generate_digest(subreddit_posts, date_fr)

    print("Envoi sur Slack...")
    post_to_slack(digest)
    print("Terminé.")


if __name__ == "__main__":
    main()
