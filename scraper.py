#!/usr/bin/env python3
"""
LinkedIn Watch — scrape the top 3 most-engaged posts (last 15 days) per profile.

Usage:
    python scraper.py                     # login window opens on first run
    python scraper.py --headless          # headless after cookies are saved
    python scraper.py --debug             # saves page.html + screenshot per profile
    python scraper.py --days 15 --top 3
"""
import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Set

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout


LINKEDIN_BASE = "https://www.linkedin.com"
COOKIE_FILE = Path("linkedin_cookies.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_profiles(path: Path) -> List[str]:
    slugs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"linkedin\.com/in/([^/?\s]+)", line)
        slugs.append(m.group(1) if m else line)
    return slugs


def parse_relative_time(text: str) -> Optional[datetime]:
    """Parse LinkedIn relative timestamps like '2d', '1w', '3h' → UTC datetime."""
    now = datetime.now(timezone.utc)
    text = text.lower().strip()
    if not text or text in ("now", "just now"):
        return now
    m = re.search(r"(\d+)\s*([smhdw])", text)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    delta_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return now - timedelta(**{delta_map[unit]: value})


def parse_count(text: str) -> int:
    """Parse '1,234', '2.3K', '1M' → int."""
    text = re.sub(r"[^\d.KkMm]", "", text.strip())
    m = re.fullmatch(r"([\d.]+)([KkMm]?)", text)
    if not m:
        return 0
    value = float(m.group(1))
    suffix = m.group(2).upper()
    return int(value * {"K": 1_000, "M": 1_000_000}.get(suffix, 1))


# ---------------------------------------------------------------------------
# Browser / auth
# ---------------------------------------------------------------------------

def save_cookies(context: BrowserContext, path: Path) -> None:
    path.write_text(json.dumps(context.cookies(), indent=2))


def load_cookies(context: BrowserContext, path: Path) -> None:
    context.add_cookies(json.loads(path.read_text()))


def ensure_logged_in(page: Page, context: BrowserContext, cookie_file: Path) -> None:
    page.goto(f"{LINKEDIN_BASE}/feed/", wait_until="domcontentloaded", timeout=30_000)

    if "/login" in page.url or "/authwall" in page.url or page.locator("input#username").is_visible():
        print(
            "\n[!] LinkedIn requires you to log in.\n"
            "    Complete the login in the browser window, then press Enter here.",
            flush=True,
        )
        input()
        page.wait_for_url(f"{LINKEDIN_BASE}/feed/**", timeout=120_000)

    save_cookies(context, cookie_file)
    print(f"[✓] Authenticated — cookies saved to {cookie_file}")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# Candidate selectors for post containers — tried in order, first match wins.
# LinkedIn changes its class names frequently; using data-urn is more stable.
POST_CONTAINER_SELECTORS = [
    "[data-urn*='urn:li:activity']",          # most reliable: data attribute with activity URN
    "[data-id*='urn:li:activity']",
    "div.occludable-update",                   # feed items (2024+)
    "div.feed-shared-update-v2",               # older class name
]

TEXT_SELECTORS = [
    "div.feed-shared-text span[dir]",
    "div.attributed-text-segment-list__content",
    "div.feed-shared-update-v2__description",
    "span.break-words",
    "div.update-components-text",
]

TIME_SELECTORS = [
    "span.update-components-actor__sub-description span[aria-hidden='true']",
    "span.feed-shared-actor__sub-description span[aria-hidden='true']",
    "time",
]


def _first_text(container, selectors: List[str], timeout: int = 2_000) -> str:
    for sel in selectors:
        try:
            el = container.locator(sel).first
            if el.count() > 0:
                return el.inner_text(timeout=timeout).strip()
        except Exception:
            pass
    return ""


def _extract_count_from_aria(container, keyword: str) -> int:
    """Find a button/element whose aria-label contains keyword and extract the number."""
    try:
        # aria-label typically looks like "123 comments" or "View 1,234 reactions"
        els = container.locator(f"[aria-label*='{keyword}']").all()
        for el in els:
            label = el.get_attribute("aria-label") or ""
            m = re.search(r"([\d,. KkMm]+)", label)
            if m:
                return parse_count(m.group(1))
    except Exception:
        pass
    return 0


def scrape_profile(page: Page, slug: str, since: datetime, debug: bool = False) -> List[dict]:
    url = f"{LINKEDIN_BASE}/in/{slug}/recent-activity/shares/"
    print(f"  → {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        print("    [!] Timeout loading page")
        return []

    # Wait for at least one post container to appear
    loaded = False
    for sel in POST_CONTAINER_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=8_000)
            print(f"    [✓] Posts detected with selector: {sel}")
            loaded = True
            break
        except PWTimeout:
            continue

    if not loaded:
        print("    [!] No post containers found — page may require different selectors.")
        if debug:
            _save_debug(page, slug)
        return []

    if debug:
        _save_debug(page, slug)

    # Determine which selector found posts
    active_sel = next(
        (s for s in POST_CONTAINER_SELECTORS if page.locator(s).count() > 0),
        POST_CONTAINER_SELECTORS[0],
    )

    posts: List[dict] = []
    seen_urls: Set[str] = set()
    prev_count = 0
    scroll_attempts = 0
    max_scroll = 25

    while scroll_attempts < max_scroll:
        containers = page.locator(active_sel).all()

        for container in containers:
            # Post URL
            post_url = ""
            try:
                link = container.locator("a[href*='/posts/'], a[href*='/feed/update/']").first
                href = link.get_attribute("href") or ""
                post_url = href.split("?")[0].strip()
            except Exception:
                pass

            if not post_url or post_url in seen_urls:
                continue
            seen_urls.add(post_url)

            # Date
            raw_time = _first_text(container, TIME_SELECTORS)
            post_date = parse_relative_time(raw_time) if raw_time else None

            # Skip posts older than the window (but keep date-unknown posts)
            if post_date is not None and post_date < since:
                continue

            # Text
            text = _first_text(container, TEXT_SELECTORS)[:500]

            # Engagement
            likes = _extract_count_from_aria(container, "reaction")
            if likes == 0:
                likes = _extract_count_from_aria(container, "like")
            comments = _extract_count_from_aria(container, "comment")
            shares = _extract_count_from_aria(container, "repost")
            if shares == 0:
                shares = _extract_count_from_aria(container, "share")

            posts.append({
                "url": post_url,
                "text": text,
                "date": post_date.strftime("%Y-%m-%d") if post_date else "unknown",
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "score": likes + comments + shares,
            })

        # Stop scrolling if no new containers appeared
        if len(posts) == prev_count and scroll_attempts > 0:
            break
        prev_count = len(posts)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2_500)
        scroll_attempts += 1

    print(f"    Collected {len(posts)} posts")
    return posts


def _save_debug(page: Page, slug: str) -> None:
    html_path = Path(f"debug_{slug}.html")
    png_path = Path(f"debug_{slug}.png")
    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=False)
    print(f"    [debug] Saved {html_path} and {png_path}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def generate_report(results: Dict[str, List[dict]], output_path: Path, days: int) -> None:
    lines = [
        "# LinkedIn Watch — Engagement Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"**Period:** last {days} days  ",
        f"**Profiles analysed:** {len(results)}",
        "",
        "---",
        "",
    ]

    for slug, posts in results.items():
        lines.append(f"## [{slug}]({LINKEDIN_BASE}/in/{slug}/recent-activity/shares/)")
        lines.append("")

        if not posts:
            lines.append("_No posts found in the selected period._")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue

        for rank, post in enumerate(posts, start=1):
            preview = post["text"].replace("\n", " ").strip()
            if len(preview) > 200:
                preview = preview[:200] + "…"

            lines += [
                f"### #{rank} — Score {post['score']}",
                "",
                f"| Date | Likes | Comments | Shares |",
                f"|------|-------|----------|--------|",
                f"| {post['date']} | {post['likes']} | {post['comments']} | {post['shares']} |",
                "",
                f"**URL:** [{post['url']}]({post['url']})" if post["url"] else "**URL:** _unavailable_",
                "",
                f"> {preview}" if preview else "> _(no text)_",
                "",
            ]

        lines.append("---")
        lines.append("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    print(f"\n[✓] Report written to {output_path}")
    return content


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(
    report_md: str,
    results: Dict[str, List[dict]],
    recipient: str,
    days: int,
    sender: str = "alxrnt@gmail.com",
) -> None:
    """Send the digest by email via Gmail SMTP.

    Required env vars:
        SMTP_PASSWORD — Gmail App Password for the sender account
    Optional env vars (override defaults):
        SMTP_HOST     — default: smtp.gmail.com
        SMTP_PORT     — default: 587
    """
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = sender
    password = os.environ.get("SMTP_PASSWORD", "")

    if not password:
        print(
            "\n[!] Email not sent — SMTP_PASSWORD env var is missing.\n"
            "    Generate a Gmail App Password at: myaccount.google.com/apppasswords\n"
            "    Then run:  export SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx"
        )
        return

    subject = f"LinkedIn Watch — digest {datetime.now().strftime('%Y-%m-%d')} ({days} days)"

    # Build a compact HTML body from the results
    html_parts = [
        "<html><body>",
        f"<h2>LinkedIn Watch — top posts ({days} derniers jours)</h2>",
    ]
    for slug, posts in results.items():
        html_parts.append(f"<h3><a href='{LINKEDIN_BASE}/in/{slug}/'>{slug}</a></h3>")
        if not posts:
            html_parts.append("<p><em>Aucun post trouvé.</em></p>")
            continue
        for rank, post in enumerate(posts, 1):
            preview = post["text"].replace("\n", " ").strip()[:200]
            url = post["url"] or "#"
            html_parts.append(
                f"<p><strong>#{rank} — Score {post['score']}</strong> "
                f"({post['date']}) "
                f"👍 {post['likes']} 💬 {post['comments']} 🔁 {post['shares']}<br>"
                f"<a href='{url}'>{url}</a><br>"
                f"<em>{preview}</em></p>"
            )
    html_parts.append("</body></html>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(report_md, "plain", "utf-8"))
    msg.attach(MIMEText("\n".join(html_parts), "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, recipient, msg.as_string())
        print(f"[✓] Digest sent to {recipient}")
    except Exception as exc:
        print(f"[!] Failed to send email: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LinkedIn Watch — engagement scraper")
    parser.add_argument("--profiles", default="profiles.txt")
    parser.add_argument("--output", default="report.md")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--cookies", default=str(COOKIE_FILE))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save page HTML + screenshot per profile for selector debugging",
    )
    parser.add_argument(
        "--email",
        default="alexandre@web-ia.com",
        metavar="ADDRESS",
        help="Recipient email address (default: alexandre@web-ia.com)",
    )
    parser.add_argument(
        "--smtp-user",
        default=os.environ.get("SMTP_USER", "alxrnt@gmail.com"),
        metavar="ADDRESS",
        help="Sender Gmail address (default: alxrnt@gmail.com)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip email sending even if --email is set",
    )
    args = parser.parse_args()

    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"[!] Profiles file not found: {profiles_path}")
        sys.exit(1)

    slugs = parse_profiles(profiles_path)
    if not slugs:
        print("[!] No profiles found in profiles file.")
        sys.exit(1)

    print(f"[*] Profiles: {slugs}")
    cookie_file = Path(args.cookies)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    results: Dict[str, List[dict]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        if cookie_file.exists():
            print(f"[*] Loading cookies from {cookie_file}")
            load_cookies(context, cookie_file)

        page = context.new_page()
        ensure_logged_in(page, context, cookie_file)

        for slug in slugs:
            print(f"\n[*] {slug}")
            posts = scrape_profile(page, slug, since, debug=args.debug)
            posts.sort(key=lambda p: p["score"], reverse=True)
            results[slug] = posts[: args.top]
            print(f"    → kept top {len(results[slug])}")

        browser.close()

    report_md = generate_report(results, Path(args.output), args.days)

    if args.email and not args.no_email:
        send_email(report_md, results, args.email, args.days, sender=args.smtp_user)


if __name__ == "__main__":
    main()
