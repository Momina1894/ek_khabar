"""
Ek Khabar - Step 3: The Site Builder
Reads headlines.db and writes a static website into ./site/

Pages:
  site/index.html        the cover: today's stories, most-covered first
  site/story/<id>.html   one story: every outlet's headline, loaded words highlighted
  site/words.html        Word Watch: most used loaded words, this week / this month
  site/outlets.html      outlets compared by how often they use loaded words

Usage:
    python build.py
Then open site/index.html in a browser.
"""

import html
import os
import random
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB_PATH = "headlines.db"
OUT_DIR = "site"
SITE_NAME = "Ek Khabar"
MIN_OUTLETS = 2   # a story needs this many outlets to be shown
INDEX_DAYS = 2    # a story is "current" if any outlet ran it within this many days
MIN_HEADLINES = 100  # below this an outlet's loaded-word rate is too noisy to rank on

# Opinion columns are excluded everywhere: they have no other outlet's version to
# compare against, and outlets contribute wildly different amounts of opinion to
# their feeds, which would skew any per-outlet comparison.
SKIP_SECTIONS = ("Opinion",)

# The cover runs a gradient through three of these. Every one is bright enough to
# hold black text. Each is stored with its hue so the day's three can be forced
# apart on the colour wheel - see day_colours.
PALETTE = [
    ("#ffd400", 50),   # yellow
    ("#b6ff3c", 85),   # acid green
    ("#5cff5c", 120),  # green
    ("#00e5a8", 164),  # mint
    ("#4de1ff", 193),  # cyan
    ("#6f8dff", 228),  # periwinkle
    ("#c9a4ff", 264),  # lavender
    ("#f06bff", 296),  # magenta
    ("#ff4f87", 340),  # rose
]

MIN_HUE_GAP = 60  # degrees between any two of the day's colours


def hue_gap(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def day_colours(n=4):
    """
    The day's colours, one per band. Seeded on the date, so the cover is a
    different combination each morning but does not reshuffle on every
    half-hourly rebuild.

    They are forced apart on the colour wheel. Sampling freely kept drawing
    neighbours - yellow, acid green and mint in one run - which is technically a
    gradient but barely travels anywhere. The palette covers the wheel evenly on
    purpose: with only six colours there were just four legal four-way
    combinations and all of them shared the same two, so every day looked alike.
    """
    rng = random.Random(utc_now().strftime("%Y-%m-%d"))
    for _ in range(400):
        pick = rng.sample(PALETTE, n)
        if all(hue_gap(pick[i][1], pick[j][1]) >= MIN_HUE_GAP
               for i in range(n) for j in range(i + 1, n)):
            return [c for c, _ in pick]
    return [c for c, _ in rng.sample(PALETTE, n)]  # pathological seed, take anything


# ---------------------------------------------------------------- loaded words

def load_words(path="loaded_words.txt"):
    words = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                words.append(line)
    # longest first so "major blow" wins over "blow"
    words.sort(key=len, reverse=True)
    return words


LOADED = load_words()
LOADED_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) + r"(?:s|es|ed|ing)?" for w in LOADED) + r")\b",
    re.IGNORECASE,
)


def find_loaded(title):
    return [m.group(0).lower() for m in LOADED_RE.finditer(title)]


def highlight(title):
    """Escape HTML and wrap loaded words in <mark>."""
    out, last = [], 0
    for m in LOADED_RE.finditer(title):
        out.append(html.escape(title[last:m.start()]))
        out.append(f"<mark>{html.escape(m.group(0))}</mark>")
        last = m.end()
    out.append(html.escape(title[last:]))
    return "".join(out)


# ---------------------------------------------------------------- data

def utc_now():
    return datetime.now(timezone.utc)


def load_stories(conn, days=INDEX_DAYS):
    rows = conn.execute("""
        SELECT s.story_id, h.id, h.outlet, h.title, h.url, h.section,
               COALESCE(h.published, h.fetched_at) AS ts
        FROM stories s JOIN headlines h ON h.id = s.headline_id
        ORDER BY ts
    """).fetchall()
    stories = defaultdict(list)
    for sid, hid, outlet, title, url, section, ts in rows:
        stories[sid].append({"id": hid, "outlet": outlet, "title": title,
                             "url": url, "section": section, "ts": ts})

    cutoff = (utc_now() - timedelta(days=days)).isoformat()
    result = []
    for sid, items in stories.items():
        outlets = {i["outlet"] for i in items}
        if len(outlets) < MIN_OUTLETS:
            continue
        # Rows are ordered by timestamp, so the last item is the most recent.
        # A story is current if any outlet was still running it inside the window.
        last_ts = items[-1]["ts"]
        if last_ts < cutoff:
            continue
        # The "plainest" headline stands in as the story title: fewest loaded words, then shortest.
        plain = min(items, key=lambda i: (len(find_loaded(i["title"])), len(i["title"])))
        # The opposite end: whoever reached for the most loaded language.
        loudest = max(items, key=lambda i: (len(find_loaded(i["title"])), len(i["title"])))
        desks = Counter(i["section"] for i in items if i["section"])
        result.append({
            "id": sid,
            "title": plain["title"],
            "plain": plain,
            "loudest": loudest,
            "items": items,
            "outlets": len(outlets),
            "section": desks.most_common(1)[0][0] if desks else "",
            "loaded_count": sum(len(find_loaded(i["title"])) for i in items),
            "first_ts": items[0]["ts"],
            "last_ts": last_ts,
        })
    # Two stable passes: newest first, then re-ordered by how widely it was covered.
    # Stories tied on coverage therefore come out most-recently-updated first.
    result.sort(key=lambda s: s["last_ts"], reverse=True)
    result.sort(key=lambda s: (-s["outlets"], -len(s["items"])))
    return result


def load_headlines_since(conn, days):
    since = (utc_now() - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT outlet, title, url FROM headlines "
        "WHERE COALESCE(published, fetched_at) >= ? "
        "  AND (section IS NULL OR section NOT IN (%s))"
        % ",".join("?" * len(SKIP_SECTIONS)),
        (since, *SKIP_SECTIONS),
    ).fetchall()


def word_counts(rows):
    counts, by_outlet, examples = Counter(), defaultdict(Counter), defaultdict(list)
    for outlet, title, url in rows:
        for w in find_loaded(title):
            base = w
            counts[base] += 1
            by_outlet[base][outlet] += 1
            if len(examples[base]) < 3:
                examples[base].append((outlet, title, url))
    return counts, by_outlet, examples


def outlet_stats(rows):
    total, loaded = Counter(), Counter()
    for outlet, title, _ in rows:
        total[outlet] += 1
        loaded[outlet] += len(find_loaded(title))
    stats = [
        {"outlet": o, "headlines": total[o], "loaded": loaded[o],
         "per_100": round(100 * loaded[o] / total[o], 1) if total[o] else 0,
         "thin": total[o] < MIN_HEADLINES}
        for o in total
    ]
    # Outlets we have too few headlines from are listed but never ranked above
    # the rest: a handful of headlines can swing the rate by tens of points.
    stats.sort(key=lambda s: (s["thin"], -s["per_100"]))
    return stats


# ---------------------------------------------------------------- html

FONTS = ("https://fonts.googleapis.com/css2?"
         "family=Archivo:wght@400;500;600;700;800&"
         "family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500;6..72,600&"
         "family=Space+Mono:wght@400;700&display=swap")

CSS = """
:root {
  --paper:  #f2f1ed;
  --ink:    #12110f;
  --soft:   #6f6c66;
  --rule:   #cbc8c1;
  --marker: #ffe86b;

  --serif: "Newsreader", "Times New Roman", Times, serif;
  --sans:  "Archivo", "Helvetica Neue", Helvetica, Arial, sans-serif;
  --mono:  "Space Mono", "Courier New", ui-monospace, monospace;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
/* Anyone who has asked their system not to animate should not be dragged down
   the page either - jump them straight there. */
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
[id] { scroll-margin-top: .5rem; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: var(--serif); font-size: 17px; line-height: 1.45;
}
a { color: inherit; text-decoration: none; }
mark { background: var(--marker); color: var(--ink); padding: 0 .12em; }

/* ============================================================ THE COVER
   Full-bleed colour bands with the gradient living inside the pull quotes,
   so body text never has to sit on the muddy middle of a gradient. */
.cover { color: var(--ink); }
.frame { max-width: 1180px; margin: 0 auto;
         border-left: 1px solid var(--ink); border-right: 1px solid var(--ink); }
.band-1 { background: var(--c1); }
.band-2 { background: var(--c2); }
.band-3 { background: var(--c3); }
.band-4 { background: var(--c4); }
/* The colour change is the whole event now - no text rides on top of it. Bounded
   rather than pure vh: three of these at 34vh each is most of a screen given over
   to empty gradient on a tall monitor. */
.fade { height: clamp(170px, 22vh, 290px); border-bottom: 1px solid var(--ink); }
.fade-1 { background: linear-gradient(180deg, var(--c1) 0%, var(--c2) 100%); }
.fade-2 { background: linear-gradient(180deg, var(--c2) 0%, var(--c3) 100%); }
.fade-3 { background: linear-gradient(180deg, var(--c3) 0%, var(--c4) 100%); }

.topbar { display: flex; align-items: center; justify-content: space-between;
          gap: 1rem; padding: .8rem 1rem; }
.topbar nav { display: flex; gap: 1.1rem; }
.topbar a, .topbar .pill { font-family: var(--mono); font-size: .6rem;
          letter-spacing: .14em; text-transform: uppercase; }
.topbar a:hover { text-decoration: underline; text-underline-offset: .25em; }
.topbar .pill { border: 1px solid var(--ink); border-radius: 999px; padding: .28rem .9rem; }
.topbar .ek { font-family: var(--sans); font-weight: 800; font-size: 1.05rem;
              letter-spacing: -.04em; border: 1px solid var(--ink); padding: .12rem .5rem; }

.cells { display: grid; grid-template-columns: repeat(8, 1fr); border-top: 1px solid var(--ink); }
.cells div { height: 2.2rem; border-right: 1px solid var(--ink); }
.cells div:last-child { border-right: 0; }
@media (max-width: 700px) { .cells { grid-template-columns: repeat(4, 1fr); } }

.hero { position: relative; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); }
.art { aspect-ratio: 16 / 8.5; background: rgba(255,255,255,.17); }
@media (max-width: 700px) { .art { aspect-ratio: 4 / 5; } }
.hero-title { position: absolute; inset: 0; margin: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center; gap: .4rem;
  font-family: var(--serif); font-weight: 400; letter-spacing: -.035em; line-height: .9;
  font-size: clamp(2.6rem, 11vw, 7.4rem); padding: 3.6rem 1rem; }
.hero-title small { font-family: var(--mono); font-size: .6rem; letter-spacing: .18em;
                    text-transform: uppercase; }
.hero-meta { position: absolute; top: 1.4rem; width: 100%; display: flex;
             justify-content: space-between; padding: 0 1.2rem; pointer-events: none; }
.hero-meta div { font-family: var(--mono); font-size: .58rem; line-height: 1.6;
                 letter-spacing: .12em; text-transform: uppercase; }
.hero-meta .r { text-align: right; }

.sec { position: relative; border-bottom: 1px solid var(--ink);
       display: grid; grid-template-columns: 6.5rem 1fr 6.5rem; }
.sec .marker { border-right: 1px solid var(--ink); }
.sec .marker b { display: block; width: 1.7rem; margin: .5rem 0 0 .5rem;
                 border: 1px solid var(--ink); text-align: center;
                 font-family: var(--mono); font-weight: 400; font-size: .55rem; line-height: 1.6; }
.sec .tail { border-left: 1px solid var(--ink); }
.sec .inner { padding: 2.6rem 2rem; min-width: 0; }
@media (max-width: 860px) {
  .sec { grid-template-columns: 2.6rem 1fr; }
  .sec .tail { display: none; }
  .sec .inner { padding: 1.8rem 1.1rem; }
}
.lead { font-family: var(--serif); font-size: clamp(1.25rem, 2.5vw, 1.9rem);
        line-height: 1.26; letter-spacing: -.015em; margin: 0; max-width: 30ch; }
.sec p.body { font-size: 1rem; line-height: 1.6; max-width: 62ch; margin: 1.3rem 0 0; }
.sec h2 { font-family: var(--mono); font-weight: 400; font-size: .6rem; letter-spacing: .16em;
          text-transform: uppercase; margin: 0 0 1.4rem; }

/* word watch and outlets, sitting on a colour band rather than white paper */
.cover .bars { list-style: none; padding: 0; margin: 0;
               border-top: 1px solid rgba(18,17,15,.28); }
.cover .bars > li { display: grid; grid-template-columns: 9rem 1fr 3rem;
                    align-items: center; gap: .9rem; padding: .5rem 0; }
.cover .bars .w { font-family: var(--sans); font-weight: 700; font-size: .82rem;
                  text-transform: uppercase; letter-spacing: .02em; }
.cover .bars .bar { height: .8rem; background: var(--ink); }
.cover .bars .n { font-family: var(--mono); font-size: .74rem; text-align: right; }
.cover details { border-bottom: 1px solid rgba(18,17,15,.28); padding-bottom: .5rem; }
.cover summary { cursor: pointer; font-family: var(--mono); font-size: .62rem;
                 letter-spacing: .06em; opacity: .78; }
.cover details ul { margin: .5rem 0 0; padding-left: 1.1rem; font-size: .92rem; }
.cover details li { margin-bottom: .25rem; }
.cover details a { border-bottom: 1px solid rgba(18,17,15,.4); }
.cover .tabs { margin: 0 0 1.2rem; display: flex; gap: .4rem; }
.cover .tabs button { font-family: var(--sans); font-weight: 600; font-size: .68rem;
                      letter-spacing: .06em; text-transform: uppercase; background: none;
                      border: 1px solid var(--ink); padding: .38rem .85rem; cursor: pointer;
                      color: var(--ink); }
.cover .tabs button[aria-pressed="true"] { background: var(--ink); color: var(--paper); }
.cover table { border-collapse: collapse; width: 100%; }
.cover th, .cover td { text-align: left; padding: .6rem .5rem;
                       border-bottom: 1px solid rgba(18,17,15,.28); }
.cover th { font-family: var(--sans); font-weight: 600; font-size: .64rem;
            letter-spacing: .08em; text-transform: uppercase; opacity: .75; }
.cover td { font-size: .94rem; }
.cover td.num, .cover th.num { text-align: right; }
.cover td.num { font-family: var(--mono); font-size: .82rem; }
.cover td small { display: block; font-family: var(--mono); font-size: .64rem;
                  letter-spacing: .04em; opacity: .7; }
.cover .foot-note { margin: 1.6rem 0 0; font-size: .82rem; max-width: 54ch; opacity: .8; }
.hidden { display: none; }

/* the cover's story list */
.cover-list { list-style: none; margin: 0; padding: 0; }
.cover-list li { position: relative; border-top: 1px solid rgba(18,17,15,.3);
                 display: grid; grid-template-columns: 4.2rem 1fr 7.5rem;
                 gap: 1.2rem; align-items: baseline; padding: .95rem .4rem;
                 transition: background .12s linear, color .12s linear; }
.cover-list li:first-child { border-top: 0; }
.cover-list li:hover, .cover-list li:focus-within { background: var(--ink); color: var(--paper); }
.cover-list li:hover .ct-kicker, .cover-list li:hover .ct-meta,
.cover-list li:focus-within .ct-kicker, .cover-list li:focus-within .ct-meta { opacity: 1; }
.cover-list .ct-no { font-family: var(--mono); font-size: .58rem; letter-spacing: .1em; }
.cover-list .ct-kicker { font-family: var(--mono); font-size: .55rem; letter-spacing: .14em;
                         text-transform: uppercase; opacity: .72; margin-bottom: .2rem; }
.cover-list .ct-title { font-family: var(--serif); font-size: 1.32rem; line-height: 1.16;
                        letter-spacing: -.012em; }
.cover-list .ct-title a::after { content: ""; position: absolute; inset: 0; }
.cover-list .ct-meta { font-family: var(--mono); font-size: .55rem; letter-spacing: .08em;
                       text-transform: uppercase; text-align: right; opacity: .72; line-height: 1.7; }
@media (max-width: 700px) {
  .cover-list li { grid-template-columns: 2.6rem 1fr; gap: .4rem .8rem; }
  .cover-list .ct-meta { grid-column: 2; text-align: left; }
  .cover-list .ct-title { font-size: 1.12rem; }
}

.docfoot { display: grid; grid-template-columns: 1fr 1fr; }
.docfoot div { padding: 1.6rem 1.2rem; font-family: var(--mono); font-size: .6rem;
               line-height: 1.7; letter-spacing: .1em; text-transform: uppercase; }
.docfoot div:first-child { border-right: 1px solid var(--ink); }
.docfoot a { border-bottom: 1px solid var(--ink); }

/* ============================================================ INNER PAGES */
.masthead { padding: 1.5rem 1.4rem 1rem; border-bottom: 1px solid var(--ink); }
.wordmark { display: flex; align-items: center; gap: .08em; line-height: .86;
  font-family: var(--sans); font-weight: 800; letter-spacing: -.035em;
  font-size: clamp(2.4rem, 8vw, 4.6rem); }
.wordmark .disc { display: inline-flex; align-items: center; justify-content: center;
  width: .92em; height: .92em; border-radius: 50%; background: var(--ink); color: var(--paper);
  margin: 0 .06em; font-size: .82em; padding-bottom: .06em; }
.wordmark .serif { font-family: var(--serif); font-weight: 400; letter-spacing: -.02em; }
.tagline { font-family: var(--sans); font-weight: 600; font-size: .78rem; line-height: 1.35;
           margin-top: .7rem; max-width: 30rem; }
.tagline span { color: var(--soft); }
nav.top { padding: .7rem 1.4rem; border-bottom: 1px solid var(--rule); display: flex; gap: 1.4rem; }
nav.top a { font-family: var(--sans); font-weight: 600; font-size: .74rem;
            letter-spacing: .04em; text-transform: uppercase; color: var(--soft); }
nav.top a[aria-current], nav.top a:hover { color: var(--ink); }

/* story head */
.storyhead { padding: 2.2rem 1.4rem 1.4rem; border-bottom: 1px solid var(--ink); }
.storyhead .eyebrow { font-family: var(--mono); font-size: .58rem; letter-spacing: .16em;
                      text-transform: uppercase; color: var(--soft); }
.storyhead h1 { font-family: var(--serif); font-weight: 400; margin: .5rem 0 0;
                font-size: clamp(1.9rem, 5vw, 3.4rem); line-height: 1.03;
                letter-spacing: -.025em; max-width: 22ch; }
.storyhead .facts { margin-top: 1rem; font-family: var(--mono); font-size: .6rem;
                    letter-spacing: .1em; text-transform: uppercase; color: var(--soft); }

/* the Off Mute row table - one row per outlet's version of the story */
.rows { list-style: none; margin: 0; padding: 0; }
.rows > li { position: relative; display: grid; align-items: center;
  grid-template-columns: 5rem minmax(0,1fr) 6.5rem; gap: 1.2rem;
  padding: 1rem 1.4rem; border-bottom: 1px solid var(--rule);
  transition: background .12s linear; }
/* Each row carries its own --hov, one of the cover's colours for the day. */
.rows > li:hover, .rows > li:focus-within { background: var(--hov); }
.rows > li:hover .kicker, .rows > li:hover .count,
.rows > li:focus-within .kicker, .rows > li:focus-within .count { color: var(--ink); }
.rows > li:hover mark, .rows > li:focus-within mark {
  background: var(--ink); color: var(--hov); }
.no     { font-family: var(--mono); font-size: .58rem; letter-spacing: .1em; }
.kicker { font-family: var(--mono); font-size: .55rem; letter-spacing: .14em;
          text-transform: uppercase; color: var(--soft); margin-bottom: .25rem;
          transition: color .12s linear; }
.title  { font-family: var(--serif); font-weight: 400; font-size: 1.34rem;
          line-height: 1.18; letter-spacing: -.012em; }
.title a::after { content: ""; position: absolute; inset: 0; }
.count  { font-family: var(--mono); font-size: .55rem; letter-spacing: .08em;
          text-transform: uppercase; text-align: right; color: var(--soft);
          transition: color .12s linear; }
.rows > li.muted { color: var(--soft); }
.rows > li.muted:hover { background: none; }
@media (max-width: 860px) {
  .rows > li { grid-template-columns: 3.2rem minmax(0,1fr); gap: .4rem 1rem; }
  .rows > li > .count { grid-column: 2; text-align: left; }
  .title { font-size: 1.14rem; }
}
.backline { padding: 1.6rem 1.4rem 4rem; font-family: var(--mono); font-size: .6rem;
            letter-spacing: .1em; text-transform: uppercase; color: var(--soft); }
.backline a { border-bottom: 1px solid var(--rule); color: var(--ink); }

"""


def document(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - {SITE_NAME}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


def masthead(current, depth=0):
    # Word watch and Outlets are sections of the cover now, not pages of their
    # own, so these are anchors. Arriving from a story page jumps straight to
    # them; on the cover itself the browser scrolls smoothly.
    root = "../" * depth
    items = [("index.html", "Today", "today"),
             ("index.html#words", "Word watch", "words"),
             ("index.html#outlets", "Outlets", "outlets")]
    nav = "".join(
        f'<a href="{root}{href}"{" aria-current=\"page\"" if key == current else ""}>{label}</a>'
        for href, label, key in items
    )
    return f"""<header class="masthead">
  <a class="wordmark" href="{root}index.html" aria-label="{SITE_NAME}">
    <span>E</span><span class="disc">k</span><span class="serif">KHABAR</span>
  </a>
  <p class="tagline">One event, every headline.<br>
  <span>Pakistani outlets side by side, so you can see the wording change.</span></p>
</header>
<nav class="top">{nav}</nav>"""


def fmt_time(ts):
    try:
        dt = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=5)))  # PKT
        return dt.strftime("%d %b, %H:%M")
    except Exception:
        return ""


def fmt_date(ts):
    try:
        dt = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=5)))
        return dt.strftime("%d %b %Y")
    except Exception:
        return ""


def plural(n, word, many=None):
    """Pass `many` for anything that does not just take an -s (story/stories)."""
    if n == 1:
        return f"{n} {word}"
    return f"{n} {many or word + 's'}"


# ---------------------------------------------------------------- the cover

def cover_rows(stories, start=1):
    out = []
    for n, s in enumerate(stories, start):
        kicker = s["section"] or "Story"
        if s["loaded_count"]:
            kicker += f' &middot; {plural(s["loaded_count"], "loaded word")}'
        out.append(
            f'<li><div class="ct-no">{n:02d}</div>'
            f'<div><div class="ct-kicker">{kicker}</div>'
            f'<div class="ct-title"><a href="story/{s["id"]}.html">'
            f'{html.escape(s["title"])}</a></div></div>'
            f'<div class="ct-meta">{s["outlets"]} outlets<br>{len(s["items"])} headlines</div></li>'
        )
    return "".join(out)


def build_index(conn, stories, built_at, colours):
    c1, c2, c3, c4 = colours

    empty = ('<li class="ct-empty">No story has been picked up by two outlets yet. '
             'Check back after a few collection runs.</li>')

    week = bars_html(*word_counts(load_headlines_since(conn, 7)))
    month = bars_html(*word_counts(load_headlines_since(conn, 30)))
    outlet_rows = "".join(
        f'<tr><td>{html.escape(s["outlet"])}'
        f'{"<small>too few to rank</small>" if s["thin"] else ""}</td>'
        f'<td class="num">{s["headlines"]}</td>'
        f'<td class="num">{s["loaded"]}</td><td class="num">{s["per_100"]}</td></tr>'
        for s in outlet_stats(load_headlines_since(conn, 30))
    )

    body = f"""<div class="cover" style="--c1:{c1};--c2:{c2};--c3:{c3};--c4:{c4}">

<div class="band-1"><div class="frame">
  <div class="topbar">
    <nav><a href="#words">Word watch</a><a href="#outlets">Outlets</a></nav>
    <span class="pill">Today</span>
    <span class="ek">EK</span>
  </div>
  <div class="cells"><div></div><div></div><div></div><div></div><div></div><div></div><div></div><div></div></div>

  <header class="hero">
    <!-- Artwork slot: drop the Fresco piece in here as <img> or <video>, full-bleed. -->
    <div class="art"></div>
    <div class="hero-meta">
      <div>Updated<br>{html.escape(built_at)}</div>
      <div class="r">{plural(len(stories), "story", "stories")}<br>across Pakistani press</div>
    </div>
    <h1 class="hero-title">Ek Khabar<small>One event, every headline</small></h1>
  </header>

  <section class="sec">
    <div class="marker"><b>01</b></div>
    <div class="inner">
      <p class="lead">The same event, filed by every outlet that ran it &mdash; so you can
      watch the wording change from desk to desk.</p>
      <p class="body">Every story below was covered by at least {MIN_OUTLETS} Pakistani outlets in
      the last {INDEX_DAYS} days. The title standing in for each one is not ours: it is simply the
      plainest headline in the set, the version reaching for the fewest loaded words. Open a
      story to read all of them side by side.</p>
    </div>
    <div class="tail"></div>
  </section>
</div></div>

<div class="fade fade-1"></div>

<div class="band-2"><div class="frame">
  <section class="sec" id="stories">
    <div class="marker"><b>02</b></div>
    <div class="inner">
      <h2>Today's stories</h2>
      <ul class="cover-list">{cover_rows(stories) or empty}</ul>
    </div>
    <div class="tail"></div>
  </section>
</div></div>

<div class="fade fade-2"></div>

<div class="band-3"><div class="frame">
  <section class="sec" id="words">
    <div class="marker"><b>03</b></div>
    <div class="inner">
      <h2>Word watch</h2>
      <p class="lead">The loaded words Pakistani outlets reach for most often.</p>
      <p class="body">Open a row to see which desks used it, and three headlines it ran in.</p>
      <div class="tabs">
        <button aria-pressed="true" onclick="show('week',this)">This week</button>
        <button aria-pressed="false" onclick="show('month',this)">This month</button>
      </div>
      <div id="week">{week}</div>
      <div id="month" class="hidden">{month}</div>
      <p class="foot-note">Opinion columns are left out. Counting a word is not the same as
      calling it wrong &mdash; sometimes a crisis really is a crisis.</p>
    </div>
    <div class="tail"></div>
  </section>
</div></div>

<div class="fade fade-3"></div>

<div class="band-4"><div class="frame">
  <section class="sec" id="outlets">
    <div class="marker"><b>04</b></div>
    <div class="inner">
      <h2>Outlets</h2>
      <p class="lead">Who reaches for loaded language, and how often.</p>
      <p class="body">Last 30 days, news sections only. "Per 100" is loaded words per hundred
      headlines, so outlets running very different volumes can still be compared.</p>
      <table>
      <thead><tr><th>Outlet</th><th class="num">Headlines</th>
      <th class="num">Loaded</th><th class="num">Per 100</th></tr></thead>
      <tbody>{outlet_rows}</tbody>
      </table>
      <p class="foot-note">This measures word choice in headlines only. It says nothing about
      whether the reporting is accurate. Outlets we have fewer than {MIN_HEADLINES} headlines
      from are shown but not ranked, because at that sample size a few headlines move the rate
      by tens of points.</p>
    </div>
    <div class="tail"></div>
  </section>
  <div class="docfoot">
    <div>Ek Khabar<br>Collected every 30 minutes</div>
    <div><a href="#words">Word watch</a> &nbsp; <a href="#outlets">Outlets</a></div>
  </div>
</div></div>

</div>
<script>
function show(id, btn) {{
  for (const s of ['week','month']) document.getElementById(s).classList.toggle('hidden', s !== id);
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-pressed', b === btn);
}}
</script>"""
    return document("Today", body)


# ---------------------------------------------------------------- one story

def build_story(s, colours):
    # Each row flashes one of the cover's colours on hover. Picking independently
    # at random clumps badly - one story came out seven rose rows out of eight - so
    # shuffle the set per story and deal them round-robin. Still unpredictable
    # between stories, but every colour shows up and no two neighbours match.
    order = list(colours)
    random.Random(f'{s["id"]}').shuffle(order)
    rows = []
    for n, i in enumerate(s["items"], 1):
        hov = order[(n - 1) % len(order)]
        rows.append(
            f'<li style="--hov:{hov}">'
            f'<div class="no">NO {n:02d}</div>'
            f'<div><div class="kicker">{html.escape(i["outlet"])}</div>'
            f'<div class="title"><a href="{html.escape(i["url"])}" rel="noopener" '
            f'target="_blank">{highlight(i["title"])}</a></div></div>'
            f'<div class="count">{fmt_time(i["ts"])}</div></li>'
        )

    loaded = ""
    if s["loaded_count"]:
        loaded = f' &middot; {plural(s["loaded_count"], "loaded word")} marked'

    body = f"""{masthead("today", depth=1)}
<header class="storyhead">
  <div class="eyebrow">{html.escape(s["section"] or "Story")}</div>
  <h1>{html.escape(s["title"])}</h1>
  <div class="facts">{plural(s["outlets"], "outlet")} &middot;
  {plural(len(s["items"]), "headline")}{loaded} &middot; updated {fmt_date(s["last_ts"])}</div>
</header>
<ul class="rows">{''.join(rows)}</ul>
<p class="backline">Every headline links to the original article at that outlet. The story
title above is the plainest version in the set, not our own summary. &nbsp;
<a href="../index.html">Back to today</a></p>"""
    return document(s["title"], body)


# ---------------------------------------------------------------- word watch

def bars_html(counts, by_outlet, examples, limit=25):
    top = counts.most_common(limit)
    if not top:
        return "<p>No loaded words found in this period.</p>"
    mx = top[0][1]
    out = ['<ul class="bars">']
    for w, n in top:
        width = max(2, round(100 * n / mx))
        who = " / ".join(f"{o} {c}" for o, c in by_outlet[w].most_common())
        ex = "".join(
            f'<li><a href="{html.escape(u)}" target="_blank" rel="noopener">{highlight(t)}</a> '
            f'<em>({html.escape(o)})</em></li>'
            for o, t, u in examples[w]
        )
        out.append(
            f'<li><span class="w">{html.escape(w)}</span>'
            f'<div class="bar" style="width:{width}%"></div><span class="n">{n}</span></li>'
            f'<details><summary>{html.escape(who)}</summary><ul>{ex}</ul></details>'
        )
    out.append("</ul>")
    return "".join(out)


# Word watch and Outlets used to be pages of their own. They are sections of the
# cover now - built inline by build_index - so only the data helpers above remain.


# ---------------------------------------------------------------- main

def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    conn = sqlite3.connect(DB_PATH)
    stories = load_stories(conn)
    built_at = utc_now().astimezone(timezone(timedelta(hours=5))).strftime("%d %b %Y, %H:%M PKT")
    colours = day_colours()

    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    write(f"{OUT_DIR}/index.html", build_index(conn, stories, built_at, colours))
    for s in stories:
        write(f"{OUT_DIR}/story/{s['id']}.html", build_story(s, colours))
    write(f"{OUT_DIR}/.nojekyll", "")
    conn.close()
    print(f"Built {len(stories)} story pages into {OUT_DIR}/  colours: {', '.join(colours)}")


if __name__ == "__main__":
    main()