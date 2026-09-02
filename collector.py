"""
Ek Khabar - Step 1: The Collector
Fetches headlines from Pakistani news RSS feeds and saves them to SQLite.
Run it manually, or on a schedule (cron / GitHub Actions) every 30 minutes.

Each outlet is read from several section feeds rather than one "home" feed, so
that no outlet is under-represented just because its front-page feed is short.
Every headline is stored with a normalised section (Pakistan, World, Business,
Sports, Opinion, ...), taken from the feed's own category tag when it has one
and otherwise from the feed it arrived on.

Usage:
    python collector.py           # fetch once and save
    python collector.py --stats   # show what's in the database
    python collector.py --feeds   # check every feed is alive
"""

import html
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

DB_PATH = "headlines.db"

# (outlet, section, url).
#
# `section` is the desk a feed represents, used when the feed's own entries carry
# no category. None means "this is a mixed feed, don't guess" - such an entry is
# stored with no section unless it carries a usable tag, and a later fetch of a
# section feed fills it in. Guessing here would be actively harmful: Dawn's home
# feed carries its opinion columns too, and labelling those "Pakistan" would push
# them into the clustering that deliberately excludes opinion.
#
# Note: The News is not listed. Its National desk (rss/1/1) has been frozen
# upstream since Nov 2025 - it still answers 200 with stale items - and no other
# path serves its domestic reporting, so it could never appear on a Pakistani
# story. Its world/sport/business desks are live if you ever want it back.
FEEDS = [
    # --- English dailies -------------------------------------------------
    ("Dawn", "Pakistan", "https://www.dawn.com/feeds/pakistan"),
    ("Dawn", "Business", "https://www.dawn.com/feeds/business"),
    ("Dawn", "World", "https://www.dawn.com/feeds/world"),
    ("Dawn", "Sports", "https://www.dawn.com/feeds/sport"),
    ("Dawn", "Opinion", "https://www.dawn.com/feeds/opinion"),
    ("Dawn", None, "https://www.dawn.com/feeds/home"),

    ("Express Tribune", None, "https://tribune.com.pk/feed/home"),

    ("The Nation", "Pakistan", "https://www.nation.com.pk/rss/national"),
    ("The Nation", "World", "https://www.nation.com.pk/rss/international"),
    ("The Nation", "Business", "https://www.nation.com.pk/rss/business"),
    ("The Nation", "Sports", "https://www.nation.com.pk/rss/sports"),
    ("The Nation", "Opinion", "https://www.nation.com.pk/rss/editorials"),

    ("Daily Times", "Pakistan", "https://dailytimes.com.pk/category/pakistan/feed/"),
    ("Daily Times", "World", "https://dailytimes.com.pk/category/world/feed/"),
    ("Daily Times", "Business", "https://dailytimes.com.pk/category/business/feed/"),
    ("Daily Times", "Sports", "https://dailytimes.com.pk/category/sports/feed/"),

    ("Minute Mirror", "World", "https://minutemirror.com.pk/category/world/feed/"),
    ("Minute Mirror", "Business", "https://minutemirror.com.pk/category/business/feed/"),
    ("Minute Mirror", None, "https://minutemirror.com.pk/feed/"),

    ("Pakistan Observer", "Business", "https://pakobserver.net/category/business/feed/"),
    ("Pakistan Observer", "Sports", "https://pakobserver.net/category/sports/feed/"),
    ("Pakistan Observer", None, "https://pakobserver.net/feed/"),

    ("Business Recorder", None, "https://www.brecorder.com/feeds/latest-news"),

    # --- broadcasters -----------------------------------------------------
    ("Geo", "Pakistan", "https://www.geo.tv/rss/1/1"),
    ("Geo", "World", "https://www.geo.tv/rss/1/2"),
    ("Geo", "Business", "https://www.geo.tv/rss/1/3"),
    ("Geo", "Sports", "https://www.geo.tv/rss/1/4"),

    ("ARY News", None, "https://arynews.tv/feed/"),

    ("BOL News", "Pakistan", "https://www.bolnews.com/category/pakistan/feed/"),
    ("BOL News", "World", "https://www.bolnews.com/category/world/feed/"),
    ("BOL News", "Business", "https://www.bolnews.com/category/business/feed/"),
    ("BOL News", "Sports", "https://www.bolnews.com/category/sports/feed/"),

    # --- state wire -------------------------------------------------------
    # The government's own account of an event. Worth having as the baseline
    # every other outlet's wording can be read against.
    ("APP", "Pakistan", "https://www.app.com.pk/category/national/feed/"),
    ("APP", "Business", "https://www.app.com.pk/category/business/feed/"),
    ("APP", "Sports", "https://www.app.com.pk/category/sports/feed/"),
]

USER_AGENT = "EkKhabarBot/0.1 (headline research project; contact: mominahumayun765@gmail.com)"

# ---------------------------------------------------------------- sections

# Outlets name their desks differently. Everything is folded into this short
# vocabulary so sections can be compared across outlets.
SECTION_MAP = {
    "pakistan": "Pakistan", "national": "Pakistan", "politics": "Pakistan",
    "punjab": "Pakistan", "sindh": "Pakistan", "karachi": "Pakistan",
    "islamabad": "Pakistan", "lahore": "Pakistan", "balochistan": "Pakistan",
    "khyber pakhtunkhwa": "Pakistan", "kp": "Pakistan", "gilgit baltistan": "Pakistan",

    "world": "World", "international": "World", "foreign": "World",

    "business": "Business", "markets": "Business", "business & finance": "Business",
    "economy": "Business", "money": "Business",

    "sports": "Sports", "sport": "Sports",

    "entertainment": "Entertainment", "showbiz": "Entertainment",
    "life & style": "Entertainment", "lifestyle": "Entertainment", "life": "Entertainment",

    "science": "Science", "technology": "Science", "tech": "Science",
    "science & technology": "Science", "sci-tech": "Science",

    "health": "Health",

    "opinion": "Opinion", "editorial": "Opinion", "editorials": "Opinion",
    "columns": "Opinion", "column": "Opinion", "blogs": "Opinion", "blog": "Opinion",
    "newspost": "Opinion", "letters": "Opinion", "comment": "Opinion",

    "weird": "Other", "amazing": "Other",
}

# Category labels that describe prominence, not subject. Ignored when picking.
NOISE_TAGS = {"must read", "top news", "editor's choice", "editors choice",
              "latest", "latest news", "featured", "home", "uncategorized"}


def normalise_section(raw):
    """Map one raw category label onto the short vocabulary, or None."""
    key = html.unescape(raw or "").strip().lower()
    if not key or key in NOISE_TAGS:
        return None
    return SECTION_MAP.get(key)


def entry_section(entry, fallback):
    """
    Best section for an entry: its own category if we recognise one, else the
    desk the feed represents. Returns None when neither is known, which is
    honest - a later fetch from a section feed will fill it in.
    """
    for tag in entry.get("tags") or []:
        # Some feeds pack several labels into a single term, e.g. "Must Read, Pakistan",
        # and WordPress feeds mix real desks in with story keywords.
        for part in (tag.get("term") or "").split(","):
            section = normalise_section(part)
            if section and section != "Other":
                return section
    return fallback


# ---------------------------------------------------------------- db

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS headlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outlet TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            published TEXT,          -- outlet's own timestamp if available (ISO 8601, UTC)
            fetched_at TEXT NOT NULL -- when we saw it (ISO 8601, UTC)
        )
    """)
    # Added later than the original schema, so existing databases need it back-filled.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(headlines)")}
    if "section" not in columns:
        conn.execute("ALTER TABLE headlines ADD COLUMN section TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outlet ON headlines(outlet)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON headlines(fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_section ON headlines(section)")
    conn.commit()


def parse_published(entry):
    """Get the entry's publish time as ISO string, or None if the feed doesn't provide one."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc).isoformat()
    return None


def get_feed(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def fetch_all(conn):
    now = datetime.now(timezone.utc).isoformat()
    # A feed that keeps serving items from months ago is broken upstream, not quiet.
    stale_before = datetime.now(timezone.utc) - timedelta(days=30)
    total_new = 0
    backfilled = 0
    per_outlet = {}

    for outlet, fallback, url in FEEDS:
        try:
            feed = get_feed(url)
        except Exception as e:
            print(f"[error] {outlet} <- {url}: {e}")
            continue

        if feed.bozo and not feed.entries:
            print(f"[warn]  {outlet} <- {url}: unreadable ({feed.get('bozo_exception', 'unknown error')})")
            continue

        new_count = 0
        fresh_seen = False
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            published = parse_published(entry)
            if published and datetime.fromisoformat(published) >= stale_before:
                fresh_seen = True
            section = entry_section(entry, fallback)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO headlines (outlet, title, url, published, fetched_at, section) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (outlet, title, link, published, now, section),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new_count += 1
                else:
                    # Already stored, but possibly from before sections existed.
                    # Back-fill it while the item is still in the feed.
                    conn.execute(
                        "UPDATE headlines SET section = ? WHERE url = ? AND section IS NULL",
                        (section, link),
                    )
                    backfilled += conn.execute("SELECT changes()").fetchone()[0]
            except sqlite3.Error as e:
                print(f"[error] {outlet}: db insert failed: {e}")

        conn.commit()
        total_new += new_count
        per_outlet[outlet] = per_outlet.get(outlet, 0) + new_count

        flag = "" if fresh_seen or not feed.entries else "  [stale feed - nothing recent]"
        print(f"[ok]    {outlet:<18} {len(feed.entries):>3} in feed, {new_count:>3} new  <- {url}{flag}")

        time.sleep(1.5)  # be polite between requests

    print(f"\nDone. {total_new} new headlines saved.")
    if backfilled:
        print(f"Back-filled sections on {backfilled} older headlines.")
    for outlet in sorted(per_outlet, key=lambda o: -per_outlet[o]):
        print(f"  {outlet:<18} {per_outlet[outlet]:>3}")


def check_feeds():
    """Ping every feed and report what came back. Use after an outlet redesigns its site."""
    print(f"{'Outlet':<18} {'Section':<14} {'Items':>5}  Newest")
    print("-" * 78)
    for outlet, fallback, url in FEEDS:
        desk = fallback or "(mixed)"
        try:
            feed = get_feed(url)
            newest = parse_published(feed.entries[0]) if feed.entries else None
            age = ""
            if newest:
                # Some outlets post-date editorials, so the age can come out negative.
                days = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(newest)).days)
                age = f"{newest[:16]}  ({days}d old)" + ("   <-- STALE" if days > 30 else "")
            print(f"{outlet:<18} {desk:<14} {len(feed.entries):>5}  {age or '-'}")
        except Exception as e:
            print(f"{outlet:<18} {desk:<14} {'FAIL':>5}  {e}")
        time.sleep(1)


def show_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
    print(f"Total headlines: {total}\n")
    print(f"{'Outlet':<20} {'Count':>6}   Latest")
    print("-" * 60)
    rows = conn.execute("""
        SELECT outlet, COUNT(*), MAX(fetched_at)
        FROM headlines GROUP BY outlet ORDER BY COUNT(*) DESC
    """).fetchall()
    for outlet, count, latest in rows:
        print(f"{outlet:<20} {count:>6}   {latest[:16] if latest else '-'}")

    print(f"\n{'Section':<20} {'Count':>6}")
    print("-" * 60)
    for section, count in conn.execute(
        "SELECT COALESCE(section, '(none)'), COUNT(*) FROM headlines "
        "GROUP BY 1 ORDER BY COUNT(*) DESC"
    ):
        print(f"{section:<20} {count:>6}")


if __name__ == "__main__":
    if "--feeds" in sys.argv:
        check_feeds()
    else:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        if "--stats" in sys.argv:
            show_stats(conn)
        else:
            fetch_all(conn)
        conn.close()