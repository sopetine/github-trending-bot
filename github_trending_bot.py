#!/usr/bin/env python3
"""
GitHub Trending Daily Bot
=========================
Fetches GitHub trending repos (daily + monthly), formats a digest message,
and sends it via Telegram Bot API.

Runs as a GitHub Actions scheduled job every day at 7 PM Tbilisi time (UTC+4 = 15:00 UTC).

Usage:
    BOT_TOKEN=... NOTIFY_CHAT_ID=... python github_trending_bot.py

Environment variables:
    BOT_TOKEN           Telegram bot token
    NOTIFY_CHAT_ID      Telegram chat/user ID to send the message to
    GH_TOKEN            GitHub token — raises rate limit from 60 to 5000/hr (required in CI)
    TOP_N               Number of repos to show per section (default: 15)
"""

import html
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
GH_TOKEN     = os.environ.get("GH_TOKEN", "")

try:
    TOP_N = int(os.environ.get("TOP_N", "15"))
except ValueError:
    sys.exit("ERROR: TOP_N must be an integer.")

MONTH_AGO   = datetime.now(timezone.utc) - timedelta(days=30)
GH_MAX_PAGE = 400  # GitHub hard pagination cap for stargazers

# Maximum total chars (monospace prefix + proportional name) before Telegram
# wraps on mobile. Calibrated conservatively from device observation.
TELEGRAM_MAX_LINE = 42

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def gh_api_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    h = {"User-Agent": "github-trending-bot/1.0", "Accept": accept}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


# ---------------------------------------------------------------------------
# Trending scraper
# ---------------------------------------------------------------------------

def fetch_trending(period: str) -> list[dict]:
    """Scrape github.com/trending for 'daily' or 'monthly'. Retries on transient errors."""
    url = f"https://github.com/trending?since={period}"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=30)
            resp.raise_for_status()
            repos = parse_trending_html(resp.text)
            # Canary: if most repos have 0 period stars the page structure likely changed
            if repos and sum(1 for r in repos if r["stars_period"] == 0) > len(repos) // 2:
                print(f"WARNING: Most repos have stars_period=0 for '{period}' — page structure may have changed.")
            return repos
        except requests.RequestException as exc:
            if attempt == 2:
                print(f"ERROR: Failed to fetch trending ({period}) after 3 attempts: {exc}")
                return []
            time.sleep(2 ** attempt)
    return []


def parse_trending_html(html_text: str) -> list[dict]:
    repos = []
    article_re = re.compile(
        r'<article\s[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
        re.DOTALL,
    )
    for article in article_re.finditer(html_text):
        block = article.group(1)

        name_m = re.search(
            r'<h2\b[^>]*>.*?<a\s[^>]*href="/([^/"]+/[^/"]+)"', block, re.DOTALL
        )
        if not name_m:
            continue
        full_name = name_m.group(1).strip()

        # Total stars (inside /stargazers link, may contain SVG)
        stars_m = re.search(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', block, re.DOTALL)
        stars_total = 0
        if stars_m:
            inner = re.sub(r"<[^>]+>", "", stars_m.group(1)).strip()
            d = re.search(r"[\d,]+", inner)
            if d:
                stars_total = int(d.group(0).replace(",", ""))

        # Period stars (float-sm-right span contains SVG before number)
        period_m = re.search(
            r'float-sm-right.*?>\s*(?:<[^>]+>\s*)*([\d,]+)\s+stars?\s+(?:today|this\s+\w+)',
            block, re.IGNORECASE | re.DOTALL,
        )
        stars_period = int(period_m.group(1).replace(",", "")) if period_m else 0

        repos.append({
            "name":         full_name,
            "url":          f"https://github.com/{full_name}",
            "stars_total":  stars_total,
            "stars_period": stars_period,
        })

    repos.sort(key=lambda r: r["stars_period"], reverse=True)
    return repos


# ---------------------------------------------------------------------------
# Monthly star lookup — binary search on stargazer pages + GraphQL fallback
# ---------------------------------------------------------------------------

def fetch_monthly_stars(repo_name: str, stars_total: int):
    """
    Count stars added in the last 30 days via binary search on stargazer pages.
    Returns: int (count), "RATE_LIMITED" (sentinel), or None (too large / error).
    """
    if stars_total == 0:
        return 0

    total_pages   = math.ceil(stars_total / 100)
    lo_bound      = max(1, total_pages - GH_MAX_PAGE + 1)
    last_accessible = min(total_pages, GH_MAX_PAGE)

    # Entire accessible window is before the search range — all recent stars
    # are beyond page 400 (inaccessible). Use arithmetic shortcut.
    if lo_bound > last_accessible:
        return max(0, stars_total - GH_MAX_PAGE * 100)

    def get_first_timestamp(page: int):
        url = (
            f"https://api.github.com/repos/{repo_name}/stargazers"
            f"?per_page=100&page={page}"
        )
        try:
            resp = requests.get(
                url,
                headers=gh_api_headers("application/vnd.github.v3.star+json"),
                timeout=10,
            )
            if resp.status_code in (403, 429):
                return "RATE_LIMITED"
            if resp.status_code == 422:
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            # Sort to guard against any ordering change from GitHub
            data.sort(key=lambda x: x.get("starred_at", ""))
            return datetime.fromisoformat(data[0]["starred_at"].replace("Z", "+00:00"))
        except (requests.RequestException, KeyError, ValueError):
            return None
        finally:
            time.sleep(0.25)

    ts_lo = get_first_timestamp(lo_bound)
    if ts_lo == "RATE_LIMITED":
        return "RATE_LIMITED"
    if ts_lo is None:
        return None
    if ts_lo >= MONTH_AGO:
        return max(0, stars_total - (lo_bound - 1) * 100)

    ts_hi = get_first_timestamp(last_accessible)
    if ts_hi == "RATE_LIMITED":
        return "RATE_LIMITED"
    if ts_hi is None:
        return None
    if ts_hi < MONTH_AGO:
        # All accessible pages are pre-MONTH_AGO — stars beyond page 400 are recent
        return max(0, stars_total - GH_MAX_PAGE * 100)

    # Binary search within [lo_bound .. last_accessible]
    lo, hi     = lo_bound, last_accessible
    boundary   = last_accessible
    while lo <= hi:
        mid = (lo + hi) // 2
        ts  = get_first_timestamp(mid)
        if ts == "RATE_LIMITED":
            return "RATE_LIMITED"
        if ts is None:
            hi = mid - 1
            continue
        if ts < MONTH_AGO:
            lo = mid + 1
        else:
            boundary = mid
            hi = mid - 1

    return max(0, stars_total - (boundary - 1) * 100)


def fetch_monthly_stars_graphql(repo_name: str):
    """
    Fallback: count stars in last 30 days via GraphQL backward pagination.
    Used when REST binary search returns None (repo too large for pagination).
    Returns: int, "RATE_LIMITED", or None on failure.
    """
    if not GH_TOKEN:
        return None

    owner, name = repo_name.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        stargazers(last: 100, before: $cursor, orderBy: {field: STARRED_AT, direction: ASC}) {
          pageInfo { hasPreviousPage startCursor }
          edges { starredAt }
        }
      }
    }
    """
    cursor    = None
    count     = 0
    max_pages = 100  # cap at 10,000 stars via this path

    for page_num in range(max_pages):
        variables: dict = {"owner": owner, "name": name}
        if cursor:
            variables["cursor"] = cursor

        try:
            resp = requests.post(
                "https://api.github.com/graphql",
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": f"Bearer {GH_TOKEN}",
                    "User-Agent": "github-trending-bot/1.0",
                },
                timeout=15,
            )
            if resp.status_code in (403, 429):
                return "RATE_LIMITED"
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None
        finally:
            time.sleep(0.3)

        if data.get("errors"):
            return None

        sg        = data.get("data", {}).get("repository", {}).get("stargazers", {})
        edges     = sg.get("edges", [])
        page_info = sg.get("pageInfo", {})

        if not edges:
            break

        done = False
        for edge in reversed(edges):  # ASC order — newest is last
            starred = edge.get("starredAt", "")
            if not starred:
                continue
            ts = datetime.fromisoformat(starred.replace("Z", "+00:00"))
            if ts >= MONTH_AGO:
                count += 1
            else:
                done = True
                break

        if done or not page_info.get("hasPreviousPage"):
            break
        cursor = page_info.get("startCursor")

    # If we exhausted max_pages without finishing, signal truncation with None
    # so the caller shows '—' rather than a silently wrong number
    else:
        return None

    return count


# ---------------------------------------------------------------------------
# Enrich repos with cross-period star counts
# ---------------------------------------------------------------------------

def enrich_cross_period(repos: list[dict], known_map: dict[str, int]) -> None:
    """
    For each repo not in known_map, fetch its cross-period star count.
    Strategy:
      1. REST binary search (~10 API calls, handles most cases)
      2. GraphQL fallback if REST returns None (very large repos)
    Stores result in repo["stars_cross"]. None = unavailable, shown as '—'.
    """
    rate_limited = False

    for repo in repos:
        if repo["name"] in known_map:
            repo["stars_cross"] = known_map[repo["name"]]
            continue

        if rate_limited:
            repo["stars_cross"] = None
            continue

        print(f"  Fetching monthly stars for {repo['name']} ({repo['stars_total']:,} total)...")
        result = fetch_monthly_stars(repo["name"], repo["stars_total"])

        if result == "RATE_LIMITED":
            print("    Rate limit hit — stopping.")
            rate_limited = True
            repo["stars_cross"] = None
        elif result is None:
            print("    REST limit reached, trying GraphQL...")
            gql = fetch_monthly_stars_graphql(repo["name"])
            if gql == "RATE_LIMITED":
                print("    GraphQL rate limit hit — stopping.")
                rate_limited = True
                repo["stars_cross"] = None
            else:
                repo["stars_cross"] = gql
                print(f"    GraphQL result: {gql}")
        else:
            repo["stars_cross"] = result


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def visual_width(s: str) -> int:
    """Display width: wide chars (emoji, CJK) count as 2."""
    w = 0
    for c in s:
        if unicodedata.east_asian_width(c) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def rpad(s: str, width: int) -> str:
    """Right-pad s with spaces to reach visual width."""
    return s + " " * max(0, width - visual_width(s))


def tlen(s: str) -> int:
    """Char count as Telegram measures it: strip U+FE0F variation selectors."""
    return len(s.replace("\ufe0f", ""))


def fmt_stars(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def build_section(header: str, repos: list[dict],
                  daily_key: str, monthly_key: str) -> list[str]:
    """
    Render one trending section. Each row:
        <code>N.  ⭐️Xk  (Yk) </code><a href="...">Name</a>

    Column widths computed from this section's data independently.
    Name trimmed (no ellipsis) to keep total line within TELEGRAM_MAX_LINE.
    """
    if not repos:
        return [header, "(no data)"]

    rows_data = []
    for i, repo in enumerate(repos, 1):
        rank_s  = f"{i}."
        daily_s = f"⭐️{fmt_stars(repo[daily_key])}"
        month_s = f"({fmt_stars(repo[monthly_key])})"
        name    = repo["name"].split("/", 1)[-1]
        rows_data.append((rank_s, daily_s, month_s, name, repo["url"]))

    w_rank  = max(visual_width(r[0]) for r in rows_data) + 2
    w_daily = max(visual_width(r[1]) for r in rows_data) + 2
    w_month = max(visual_width(r[2]) for r in rows_data) + 2

    # All rows share the same prefix width — compute once
    sample = rpad(rows_data[0][0], w_rank) + rpad(rows_data[0][1], w_daily) + rpad(rows_data[0][2], w_month)
    name_budget = max(1, TELEGRAM_MAX_LINE - tlen(sample))

    lines = [header]
    for rank_s, daily_s, month_s, name, url in rows_data:
        prefix    = html.escape(rpad(rank_s, w_rank) + rpad(daily_s, w_daily) + rpad(month_s, w_month))
        safe_name = html.escape(name[:name_budget])
        lines.append(f'<code>{prefix}</code><a href="{url}">{safe_name}</a>')
    return lines


def build_message(daily: list[dict], monthly: list[dict]) -> str:
    tbilisi_now = datetime.now(timezone(timedelta(hours=4)))
    date_str    = tbilisi_now.strftime("%d %b %Y")

    lines  = [f"<b>GitHub Trending — {date_str}</b>\n"]
    lines += build_section(
        header      = "━━━━━━ <b>📅 Daily Top 15</b> ━━━━━━",
        repos       = daily[:TOP_N],
        daily_key   = "stars_period",
        monthly_key = "stars_cross",
    )
    lines.append("")
    lines += build_section(
        header      = "━━━━━━ <b>📆 Monthly Top 15</b> ━━━━━━",
        repos       = monthly[:TOP_N],
        daily_key   = "stars_cross",
        monthly_key = "stars_period",
    )
    lines.append("")
    lines.append('<a href="https://github.com/trending">View full trending →</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def send_message(text: str) -> bool:
    """Send Telegram message. Returns True on success."""
    bot_api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    payload = {
        "chat_id":                  NOTIFY_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp   = requests.post(f"{bot_api}/sendMessage", json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            print(f"ERROR: Telegram returned ok=false: {result}")
            return False
        msg_id = result.get("result", {}).get("message_id", "?")
        print(f"Message sent. Message ID: {msg_id}")
        return True
    except (requests.RequestException, ValueError) as exc:
        print(f"ERROR: Failed to send message: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        sys.exit("ERROR: BOT_TOKEN is not set.")
    if not NOTIFY_CHAT_ID:
        sys.exit("ERROR: NOTIFY_CHAT_ID is not set.")

    print("Fetching GitHub Trending (daily)...")
    daily = fetch_trending("daily")
    print(f"  Found {len(daily)} repos")

    print("Fetching GitHub Trending (monthly)...")
    monthly = fetch_trending("monthly")
    print(f"  Found {len(monthly)} repos")

    if not daily or not monthly:
        sys.exit(
            f"ERROR: Scraping returned empty list — daily={len(daily)}, monthly={len(monthly)}. "
            "GitHub page structure may have changed."
        )

    daily_map   = {r["name"]: r["stars_period"] for r in daily}
    monthly_map = {r["name"]: r["stars_period"] for r in monthly}

    print("Resolving monthly stars for daily repos...")
    enrich_cross_period(daily, monthly_map)

    print("Resolving daily stars for monthly repos...")
    enrich_cross_period(monthly, daily_map)

    print("Building message...")
    message = build_message(daily, monthly)
    print("─" * 60)
    print(re.sub(r"<[^>]+>", "", message))
    print("─" * 60)

    print("Sending Telegram message...")
    if not send_message(message):
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    main()
