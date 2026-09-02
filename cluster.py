"""
Ek Khabar - Step 2: The Clusterer
Groups recent headlines that are about the same event into "stories".

How it works:
  1. Take every headline from the last N hours (default 48), skipping opinion pieces.
  2. Turn each headline into a vector (embedding) with a small sentence model.
  3. Group headlines whose vectors are close together.
  4. Match each new group against the stories we already know about and keep that
     story's id, so a story that outlets are still covering stays at one URL.
     Groups that match nothing get a brand new id that is never reused.

Story ids are permanent. The clustering itself is redone from scratch every run,
so groups improve as more outlets cover a story, but the id a story is given the
first time it appears is the id it keeps.

Usage:
    python cluster.py            # cluster and save
    python cluster.py --show     # print stories with 2+ outlets, biggest first
    python cluster.py --hours 72 # widen the window
    python cluster.py --repair   # one-off: fix ids from before they were stable

First run downloads the model (~90MB), after that it's cached locally.
"""

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.cluster import AgglomerativeClustering

DB_PATH = "headlines.db"
MODEL_NAME = "all-MiniLM-L6-v2"

# Headlines with cosine similarity above this are considered the same story.
# Lower = bigger, looser groups. Higher = tighter groups but more stories left ungrouped.
# Start here, then look at --show output and tune.
SIMILARITY_THRESHOLD = 0.6
MIN_WORDS = 5  # shorter headlines are usually opinion/column titles and cluster badly

# Columns and editorials are not reports of an event, so they have no other
# outlet's version to compare against. They also cluster badly: abstract titles
# like "A reckoning" and "The golden goose" sit close together in embedding
# space purely because they are short and vague.
SKIP_SECTIONS = {"Opinion"}


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
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()


def get_flag(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_flag(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def window_start(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def window_ids(conn, since):
    """Every headline in the window, clusterable or not."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM headlines WHERE COALESCE(published, fetched_at) >= ?", (since,),
    )]


def load_recent(conn, since):
    rows = conn.execute(
        "SELECT id, outlet, title FROM headlines "
        "WHERE COALESCE(published, fetched_at) >= ? "
        "  AND (section IS NULL OR section NOT IN (%s)) "
        "ORDER BY id" % ",".join("?" * len(SKIP_SECTIONS)),
        (since, *sorted(SKIP_SECTIONS)),
    ).fetchall()
    return [r for r in rows if len(r[2].split()) >= MIN_WORDS]


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
        linkage="complete",
        distance_threshold=1 - SIMILARITY_THRESHOLD,
    )
    return clustering.fit_predict(vectors)


STOPWORDS = set("""a an the and or of to in on at for with by from as is are was were be been
has have had will would can could may might must not no over under after before into
about than then this that these those it its his her their our your says said say
new latest amid against per among between during while up down out off more most""".split())


def keywords(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in STOPWORDS}


def coherent(titles):
    """A story is kept only if every headline shares at least one keyword with another headline in it."""
    if len(titles) < 2:
        return True
    kws = [keywords(t) for t in titles]
    for i, k in enumerate(kws):
        if not any(k & other for j, other in enumerate(kws) if j != i):
            return False
    return True


# ---------------------------------------------------------------- stable ids

def next_free_id(conn):
    """One past the highest id ever handed out. Ids are never recycled."""
    highest = conn.execute("SELECT MAX(story_id) FROM stories").fetchone()[0]
    return 0 if highest is None else highest + 1


def assign_story_ids(conn, groups):
    """
    Give every group of headline ids a permanent story id.

    A group keeps the id of the story most of its headlines already belonged to,
    so an unfolding story stays at one URL as outlets pile on. If two groups lay
    claim to the same old id - because a story has split in two - the larger one
    keeps it and the other starts fresh. Groups matching nothing are new stories.
    """
    known = dict(conn.execute("SELECT headline_id, story_id FROM stories").fetchall())
    counter = next_free_id(conn)

    assigned, taken = {}, set()
    # Largest first, so when a story splits the main thread keeps the original id.
    for label in sorted(groups, key=lambda g: -len(groups[g])):
        votes = Counter(known[hid] for hid in groups[label] if hid in known)
        inherited = next((sid for sid, _ in votes.most_common() if sid not in taken), None)
        if inherited is None:
            inherited, counter = counter, counter + 1
        assigned[label] = inherited
        taken.add(inherited)
    return assigned


def run(conn, hours):
    # Pinned once: if the two queries below each computed their own "now", a
    # headline on the boundary could be clustered without its old row being
    # cleared first, and the insert would collide.
    since = window_start(hours)
    rows = load_recent(conn, since)
    if not rows:
        print("No headlines in window.")
        return
    print(f"Clustering {len(rows)} headlines from the last {hours}h...")

    vectors = embed([title for _, _, title in rows])
    labels = [int(lbl) for lbl in cluster(vectors)]

    # Drop incoherent groups: give every member of a rejected group its own group.
    titles_by_label = defaultdict(list)
    for (_, _, title), lbl in zip(rows, labels):
        titles_by_label[lbl].append(title)
    next_label = max(titles_by_label) + 1
    fixed, rejected = [], 0
    for lbl in labels:
        if len(titles_by_label[lbl]) > 1 and not coherent(titles_by_label[lbl]):
            fixed.append(next_label); next_label += 1; rejected += 1
        else:
            fixed.append(lbl)
    if rejected:
        print(f"Split {rejected} headlines out of incoherent groups.")
    labels = fixed

    ids = [hid for hid, _, _ in rows]
    groups = defaultdict(list)
    for hid, lbl in zip(ids, labels):
        groups[lbl].append(hid)

    before = next_free_id(conn)
    story_id_for = assign_story_ids(conn, groups)

    now = datetime.now(timezone.utc).isoformat()
    # Clear the whole window, not just what we are about to re-insert. A headline
    # can drop out of clustering after it was first grouped - an opinion piece
    # whose section arrived on a later fetch, say - and its old row would
    # otherwise linger and keep showing up inside a story.
    stale = window_ids(conn, since)
    conn.execute(
        f"DELETE FROM stories WHERE headline_id IN ({','.join('?' * len(stale))})", stale
    )
    # Same problem for headlines that have since aged out of the window entirely:
    # nothing above would ever revisit them, so sweep skipped sections globally.
    conn.execute(
        "DELETE FROM stories WHERE headline_id IN ("
        "  SELECT id FROM headlines WHERE section IN (%s))"
        % ",".join("?" * len(SKIP_SECTIONS)),
        tuple(sorted(SKIP_SECTIONS)),
    )
    conn.executemany(
        "INSERT INTO stories (headline_id, story_id, clustered_at) VALUES (?, ?, ?)",
        [(hid, story_id_for[lbl], now) for hid, lbl in zip(ids, labels)],
    )
    conn.commit()

    # Anything this clusterer writes already uses permanent ids.
    set_flag(conn, "story_ids_stable", 1)
    conn.commit()

    fresh = sum(1 for sid in story_id_for.values() if sid >= before)
    multi = sum(1 for hids in groups.values() if len(hids) > 1)
    print(f"Done. {len(groups)} stories in window ({fresh} new, "
          f"{len(groups) - fresh} continuing), {multi} with more than one headline.")


# ---------------------------------------------------------------- repair

def repair(conn):
    """
    One-off migration for databases written before story ids were stable.

    Story ids used to be the clustering library's own labels, which restart at 0
    on every run. Rows for headlines that had fallen out of the window were left
    behind with their old label, so unrelated groups from different runs ended up
    sharing an id and were shown as one story. Each run's grouping was fine on its
    own, so this splits the table back apart by run and renumbers it.
    """
    rows = conn.execute("SELECT headline_id, story_id, clustered_at FROM stories").fetchall()
    if not rows:
        print("Nothing to repair.")
        return

    # Under stable ids a story is *expected* to span several runs - that is what
    # continuity looks like - so "spans several runs" cannot be used to detect
    # the old bug. Once the migration has been done the flag says so, and running
    # this again would tear continuing stories apart.
    if get_flag(conn, "story_ids_stable"):
        print("Already migrated: story ids are stable.\n"
              "Re-running this would split continuing stories apart, so it is a no-op.")
        return

    runs = defaultdict(set)
    for _, sid, at in rows:
        runs[sid].add(at)
    collided = sum(1 for ats in runs.values() if len(ats) > 1)

    groups = sorted({(sid, at) for _, sid, at in rows})
    renumbered = {key: i for i, key in enumerate(groups)}
    conn.executemany(
        "UPDATE stories SET story_id = ? WHERE headline_id = ?",
        [(renumbered[(sid, at)], hid) for hid, sid, at in rows],
    )
    set_flag(conn, "story_ids_stable", 1)
    conn.commit()
    print(f"Repaired {len(rows)} rows: {len(runs)} ids -> {len(groups)} stories. "
          f"{collided} ids had merged unrelated groups from different runs.")


# ---------------------------------------------------------------- show

def show(conn, min_outlets=2):
    rows = conn.execute("""
        SELECT s.story_id, h.outlet, h.title
        FROM stories s JOIN headlines h ON h.id = s.headline_id
        ORDER BY s.story_id
    """).fetchall()

    by_story = defaultdict(list)
    for sid, outlet, title in rows:
        by_story[sid].append((outlet, title))

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
    if "--repair" in sys.argv:
        repair(conn)
    elif "--show" in sys.argv:
        show(conn)
    else:
        run(conn, hours=arg_value("--hours", 48))
    conn.close()