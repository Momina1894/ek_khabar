"""
Ek Khabar - Step 2: The Clusterer
Groups recent headlines that are about the same event into "stories".

How it works:
  1. Take every headline from the last N hours (default 48).
  2. Turn each headline into a vector (embedding) with a small sentence model.
  3. Group headlines whose vectors are close together.
  4. Save the groups to a `stories` table. Groups are rebuilt from scratch each run,
     so they improve as more outlets cover a story.

Usage:
    python cluster.py            # cluster and save
    python cluster.py --show     # print stories with 2+ outlets, biggest first
    python cluster.py --hours 72 # widen the window

First run downloads the model (~90MB), after that it's cached locally.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.cluster import AgglomerativeClustering

DB_PATH = "headlines.db"
MODEL_NAME = "all-MiniLM-L6-v2"

# Headlines with cosine similarity above this are considered the same story.
# Lower = bigger, looser groups. Higher = tighter groups but more stories left ungrouped.
# Start here, then look at --show output and tune.
SIMILARITY_THRESHOLD = 0.55


def arg_value(flag, default):
    if flag in sys.argv:
        return type(default)(sys.argv[sys.argv.index(flag) + 1])
    return default


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stories (
            headline_id INTEGER PRIMARY KEY,
            story_id INTEGER NOT NULL,
            clustered_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_story ON stories(story_id)")
    conn.commit()


def load_recent(conn, hours):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT id, outlet, title FROM headlines "
        "WHERE COALESCE(published, fetched_at) >= ? ORDER BY id",
        (since,),
    ).fetchall()
    return rows


def embed(titles):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    return model.encode(titles, normalize_embeddings=True, show_progress_bar=False)


def cluster(vectors):
    """Returns a label per row. Rows with the same label are the same story."""
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    clustering = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=1 - SIMILARITY_THRESHOLD,
    )
    return clustering.fit_predict(vectors)


def run(conn, hours):
    rows = load_recent(conn, hours)
    if not rows:
        print("No headlines in window.")
        return
    print(f"Clustering {len(rows)} headlines from the last {hours}h...")

    vectors = embed([title for _, _, title in rows])
    labels = cluster(vectors)

    now = datetime.now(timezone.utc).isoformat()
    ids = [hid for hid, _, _ in rows]
    conn.execute(
        f"DELETE FROM stories WHERE headline_id IN ({','.join('?' * len(ids))})", ids
    )
    conn.executemany(
        "INSERT INTO stories (headline_id, story_id, clustered_at) VALUES (?, ?, ?)",
        [(hid, int(lbl), now) for hid, lbl in zip(ids, labels)],
    )
    conn.commit()

    n_stories = len(set(labels))
    multi = sum(1 for lbl in set(labels) if list(labels).count(lbl) > 1)
    print(f"Done. {n_stories} stories, {multi} covered by more than one headline.")


def show(conn, min_outlets=2):
    rows = conn.execute("""
        SELECT s.story_id, h.outlet, h.title
        FROM stories s JOIN headlines h ON h.id = s.headline_id
        ORDER BY s.story_id
    """).fetchall()

    by_story = {}
    for sid, outlet, title in rows:
        by_story.setdefault(sid, []).append((outlet, title))

    ranked = sorted(by_story.items(), key=lambda kv: -len({o for o, _ in kv[1]}))
    for sid, items in ranked:
        outlets = {o for o, _ in items}
        if len(outlets) < min_outlets:
            continue
        print(f"\n=== Story {sid} - {len(outlets)} outlets ===")
        for outlet, title in items:
            print(f"  [{outlet}] {title}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    if "--show" in sys.argv:
        show(conn)
    else:
        run(conn, hours=arg_value("--hours", 48))
    conn.close()
