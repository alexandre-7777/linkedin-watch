#!/usr/bin/env python3
"""Weekly Reddit editorial digest — fetches top posts via Reddit OAuth, summarizes with Claude, posts to Slack."""

import os
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

import anthropic
import praw

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


def get_reddit():
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent="RedditDigestBot/1.0 by alexandre@web-ia.com",
    )


def fetch_subreddit(reddit, subreddit: str) -> list[dict]:
    now = time.time()
    posts = []
    for submission in reddit.subreddit(subreddit).top(time_filter="week", limit=25):
        age = now - submission.created_utc
        if age > MAX_AGE_SECONDS:
            continue
        if submission.score < MIN_SCORE and submission.num_comments < MIN_COMMENTS:
            continue
        posts.append({
            "title": submission.title,
            "url": f"https://reddit.com{submission.permalink}",
            "score": submission.score,
            "num_comments": submission.num_comments,
            "selftext": submission.selftext[:200] if submission.selftext else "",
        })
    return posts


def build_posts_text(subreddit: str, posts: list[dict]) -> str:
    lines = [f"Subreddit: r/{subreddit}"]
    for p in posts[:5]:
        lines.append(
            f"- [{p['title']}]({p['url']}) | score: {p['score']} | commentaires: {p['num_comments']}"
        )
        if p["selftext"]:
            lines.append(f"  Extrait: {p['selftext']}")
    return "\n".join(lines)


def generate_digest(subreddit_posts: dict[str, list[dict]], date_fr: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content_blocks = [
        build_posts_text(sub, posts)
        for sub, posts in subreddit_posts.items()
        if posts
    ]

    prompt = f"""Tu es un rédacteur de revue de presse tech pour Alexandre.
Voici les posts Reddit notables de la semaine (score > {MIN_SCORE} ou > {MIN_COMMENTS} commentaires, moins de 7 jours) :

{chr(10).join(content_blocks)}

Rédige une revue de presse hebdomadaire en français pour Slack :
- Commence par : *Revue Reddit — semaine du {date_fr}*
- Une section par subreddit avec du contenu notable (utilise *bold* pour les titres Slack)
- Pour chaque post retenu : titre avec lien cliquable, 2-3 phrases éditoriales (sujet, pourquoi notable, réaction communauté)
- Ignore les subreddits sans posts qualifiants
- Termine par : _Prochain digest : vendredi prochain à 7h._
- Sois sélectif et éditorial : 3 excellents items valent mieux que 15 médiocres
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
    print("Démarrage de la revue Reddit hebdomadaire...")
    reddit = get_reddit()
    subreddit_posts: dict[str, list[dict]] = {}

    for sub in SUBREDDITS:
        print(f"  Fetching r/{sub}...")
        try:
            posts = fetch_subreddit(reddit, sub)
            subreddit_posts[sub] = posts
            print(f"    {len(posts)} posts qualifiants")
        except Exception as e:
            print(f"    Erreur r/{sub}: {e}")
            subreddit_posts[sub] = []

    total = sum(len(p) for p in subreddit_posts.values())
    if total == 0:
        print("Aucun post qualifiant cette semaine. Message non envoyé.")
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
