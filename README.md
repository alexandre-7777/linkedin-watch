# linkedin-watch

Scrape the **top 3 most-engaged LinkedIn posts** (last 15 days) for a list of profiles and generate a Markdown report.

## Stack

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (Chromium)

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Edit `profiles.txt` — one LinkedIn profile per line (full URL or slug):

```
williamhgates
https://www.linkedin.com/in/jeffweiner08
```

Lines starting with `#` are ignored.

## Usage

```bash
# First run — browser opens for manual login, then cookies are saved
python scraper.py

# Subsequent runs — uses saved cookies (headless)
python scraper.py --headless

# Custom options
python scraper.py \
  --profiles profiles.txt \   # input file (default: profiles.txt)
  --output report.md \        # report path  (default: report.md)
  --days 15 \                 # look-back window (default: 15)
  --top 3 \                   # posts to keep per profile (default: 3)
  --cookies linkedin_cookies.json \  # cookie file (default: linkedin_cookies.json)
  --headless                  # headless mode
```

## Output

`report.md` — one section per profile, ranked by engagement score (likes + comments + shares):

```markdown
## williamhgates

### #1 — Score: 4321
- Date: 2026-04-10
- Likes: 4000 | Comments: 200 | Shares: 121
- URL: https://www.linkedin.com/posts/…
> Post text preview…
```

## Authentication

LinkedIn requires a logged-in session. On the first run the browser opens in a non-headless window; log in normally and press Enter in the terminal. Cookies are saved to `linkedin_cookies.json` and reused automatically on subsequent runs.

> **Note:** LinkedIn's HTML structure changes frequently. If the scraper stops returning results, the CSS selectors in `scraper.py` may need updating.
