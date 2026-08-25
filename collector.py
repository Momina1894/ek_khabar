"""
Ek Khabar - Step 1: The Collector
Fetches headlines from Pakistani news RSS feeds and saves them to SQLite.
Run it manually, or on a schedule (cron / GitHub Actions) every 30 minutes.

Usage:
    python collector.py           # fetch once and save
    python collector.py --stats   # show what's in the database
"""

import sqlite3
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

DB_PATH = "headlines.db"

# Feed URLs. If one stops working, check the outlet's site for its current RSS link.
FEEDS = {
    "Dawn": "https://www.dawn.com/feeds/home",
    "Express Tribune": "https://tribune.com.pk/feed/home",
    "The News": "https://www.thenews.com.pk/rss/1/1",
    "Geo": "https://www.geo.tv/rss/1/1",
    "ARY News": "https://arynews.tv/feed/",
    "Business Recorder": "https://www.brecorder.com/feeds/latest-news"
}

USER_AGENT = "EkKhabarBot/0.1 (headline research project; contact: mominahumayun765@gmail.com)"


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outlet ON headlines(outlet)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON headlines(fetched_at)")
    conn.commit()


def parse_published(entry):
    """Get the entry's publish time as ISO string, or None if the feed doesn't provide one."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc).isoformat()
    return None


def fetch_all(conn):
    now = datetime.now(timezone.utc).isoformat()
    total_new = 0

    for outlet, url in FEEDS.items():
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[error] {outlet}: {e}")
            continue

        if feed.bozo and not feed.entries:
            print(f"[warn]  {outlet}: feed unreadable ({feed.get('bozo_exception', 'unknown error')})")
            continue

        new_count = 0
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO headlines (outlet, title, url, published, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (outlet, title, link, parse_published(entry), now),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new_count += 1
            except sqlite3.Error as e:
                print(f"[error] {outlet}: db insert failed: {e}")

        conn.commit()
        total_new += new_count
        print(f"[ok]    {outlet}: {len(feed.entries)} in feed, {new_count} new")

        time.sleep(2)  # be polite between outlets

    print(f"\nDone. {total_new} new headlines saved.")


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


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    if "--stats" in sys.argv:
        show_stats(conn)
    else:
        fetch_all(conn)
    conn.close()
