#!/usr/bin/env python3
"""
LinkedIn Watch — scrape the top 3 most-engaged posts (last 15 days) per profile.

Usage:
    python scraper.py [--profiles profiles.txt] [--output report.md] [--days 15]
    python scraper.py --headless          # run browser in headless mode
    python scraper.py --cookies cookies.json  # load saved auth cookies

LinkedIn requires authentication. On the first run, the browser opens so you can
log in manually. Cookies are then saved to linkedin_cookies.json so subsequent
runs are headless.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LINKEDIN_BASE = "https://www.linkedin.com"
COOKIE_FILE = Path("linkedin_cookies.json")


def parse_profiles(path: Path) -> list[str]:
    """Return a list of LinkedIn slugs from a profiles file."""
    slugs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept full URLs or bare slugs
        m = re.search(r"linkedin\.com/in/([^/?\s]+)", line)
        slugs.append(m.group(1) if m else line)
    return slugs


def parse_relative_time(text: str) -> datetime | None:
    """
    Parse LinkedIn's relative timestamps ("2d", "1w", "3h", "just now", …)
    into an absolute UTC datetime. Returns None when the string is unrecognised.
    """
    now = datetime.now(timezone.utc)
    text = text.lower().strip()
    if "just now" in text or "now" == text:
        return now
    m = re.search(r"(\d+)\s*([smhdw])", text)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    delta_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return now - timedelta(**{delta_map[unit]: value})


def parse_count(text: str) -> int:
    """Parse '1,234', '2.3K', '1M' etc. into an integer."""
    text = text.strip().replace(",", "").replace("\u202f", "")
    m = re.fullmatch(r"([\d.]+)\s*([KkMm]?)", text)
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
    cookies = json.loads(path.read_text())
    context.add_cookies(cookies)


def ensure_logged_in(page: Page, context: BrowserContext, cookie_file: Path) -> None:
    """Navigate to LinkedIn; if not authenticated, wait for the user to log in."""
    page.goto(f"{LINKEDIN_BASE}/feed/", wait_until="domcontentloaded")

    # Detect login wall
    if "/login" in page.url or page.locator("input#username").is_visible():
        print(
            "\n[!] LinkedIn requires you to log in.\n"
            "    Complete the login in the browser that just opened, then press Enter here.",
            flush=True,
        )
        input()
        # Wait until we're back at the feed
        page.wait_for_url(f"{LINKEDIN_BASE}/feed/**", timeout=120_000)

    save_cookies(context, cookie_file)
    print(f"[✓] Authenticated. Cookies saved to {cookie_file}")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_profile(page: Page, slug: str, since: datetime) -> list[dict]:
    """
    Visit a profile's Recent Activity page and collect posts newer than `since`.
    Returns a list of post dicts with keys: url, text, date, likes, comments, shares, score.
    """
    url = f"{LINKEDIN_BASE}/in/{slug}/recent-activity/shares/"
    print(f"  → Visiting {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        print(f"    [!] Timeout loading {url}")
        return []

    posts: list[dict] = []
    seen_urls: set[str] = set()
    scroll_attempts = 0
    max_scroll = 20  # safety limit

    while scroll_attempts < max_scroll:
        # Collect post containers currently in DOM
        containers = page.locator(
            "div.feed-shared-update-v2, div[data-urn]"
        ).all()

        new_found = False
        for container in containers:
            # --- Post URL / ID ---
            try:
                link_el = container.locator("a[href*='/posts/'], a[href*='/feed/update/']").first
                href = link_el.get_attribute("href") or ""
                post_url = href.split("?")[0]
            except Exception:
                post_url = ""

            if not post_url or post_url in seen_urls:
                continue
            seen_urls.add(post_url)

            # --- Date ---
            post_date: datetime | None = None
            try:
                time_el = container.locator("span.feed-shared-actor__sub-description span[aria-hidden]").first
                raw_time = time_el.inner_text(timeout=2_000).strip()
                post_date = parse_relative_time(raw_time)
            except Exception:
                pass

            if post_date and post_date < since:
                # Posts are roughly chronological; once we hit old content stop
                continue

            # --- Text ---
            try:
                text_el = container.locator("div.feed-shared-update-v2__description, span.break-words").first
                text = text_el.inner_text(timeout=2_000).strip()[:500]
            except Exception:
                text = ""

            # --- Engagement counts ---
            likes = comments = shares = 0
            try:
                reaction_el = container.locator("span.social-details-social-counts__reactions-count").first
                likes = parse_count(reaction_el.inner_text(timeout=2_000))
            except Exception:
                pass
            try:
                comment_el = container.locator(
                    "li.social-details-social-counts__item button[aria-label*='comment']"
                ).first
                raw = comment_el.get_attribute("aria-label") or ""
                m = re.search(r"(\d[\d,.KkMm]*)", raw)
                comments = parse_count(m.group(1)) if m else 0
            except Exception:
                pass
            try:
                share_el = container.locator(
                    "li.social-details-social-counts__item button[aria-label*='repost']"
                ).first
                raw = share_el.get_attribute("aria-label") or ""
                m = re.search(r"(\d[\d,.KkMm]*)", raw)
                shares = parse_count(m.group(1)) if m else 0
            except Exception:
                pass

            score = likes + comments + shares
            posts.append(
                {
                    "url": post_url,
                    "text": text,
                    "date": post_date.strftime("%Y-%m-%d") if post_date else "unknown",
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "score": score,
                }
            )
            new_found = True

        if not new_found:
            break

        # Scroll down to load more posts
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2_000)
        scroll_attempts += 1

    return posts


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(results: dict[str, list[dict]], output_path: Path, days: int) -> None:
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
        lines.append(f"## [{slug}]({LINKEDIN_BASE}/in/{slug}/)")
        lines.append("")

        if not posts:
            lines.append("_No posts found in the selected period._")
            lines.append("")
            continue

        for rank, post in enumerate(posts, start=1):
            text_preview = post["text"].replace("\n", " ").strip()
            if len(text_preview) > 200:
                text_preview = text_preview[:200] + "…"

            lines += [
                f"### #{rank} — Score: {post['score']}",
                "",
                f"- **Date:** {post['date']}",
                f"- **Likes:** {post['likes']} | **Comments:** {post['comments']} | **Shares:** {post['shares']}",
                f"- **URL:** [{post['url']}]({post['url']})" if post["url"] else "- **URL:** _unavailable_",
                "",
                f"> {text_preview}" if text_preview else "> _(no text)_",
                "",
            ]

        lines.append("---")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[✓] Report written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LinkedIn Watch — engagement scraper")
    parser.add_argument("--profiles", default="profiles.txt", help="Path to profiles file")
    parser.add_argument("--output", default="report.md", help="Output report path")
    parser.add_argument("--days", type=int, default=15, help="Look-back window in days")
    parser.add_argument("--cookies", default=str(COOKIE_FILE), help="Cookie file path")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--top", type=int, default=3, help="Number of top posts to keep per profile")
    args = parser.parse_args()

    profiles_path = Path(args.profiles)
    if not profiles_path.exists():
        print(f"[!] Profiles file not found: {profiles_path}")
        sys.exit(1)

    slugs = parse_profiles(profiles_path)
    if not slugs:
        print("[!] No profiles found in the profiles file.")
        sys.exit(1)

    print(f"[*] Profiles to scrape: {slugs}")
    cookie_file = Path(args.cookies)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    results: dict[str, list[dict]] = {}

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

        # Load saved cookies if available
        if cookie_file.exists():
            print(f"[*] Loading cookies from {cookie_file}")
            load_cookies(context, cookie_file)

        page = context.new_page()
        ensure_logged_in(page, context, cookie_file)

        for slug in slugs:
            print(f"\n[*] Scraping profile: {slug}")
            posts = scrape_profile(page, slug, since)
            # Sort by engagement score descending; keep top N
            posts.sort(key=lambda p: p["score"], reverse=True)
            results[slug] = posts[: args.top]
            print(f"    Found {len(posts)} posts → kept top {len(results[slug])}")

        browser.close()

    generate_report(results, Path(args.output), args.days)


if __name__ == "__main__":
    main()
