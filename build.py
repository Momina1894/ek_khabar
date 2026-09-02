"""
Ek Khabar - Step 3: The Site Builder
Reads headlines.db and writes a static website into ./site/

Pages:
  site/index.html        today's stories, most-covered first
  site/story/<id>.html   one story: every outlet's headline, loaded words highlighted
  site/words.html        Word Watch: most used loaded words, this week / this month
  site/outlets.html      outlets compared by how often they use loaded words

Usage:
    python build.py
Then open site/index.html in a browser.
"""

import html
import os
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

# Outlet names as they appear in the circled-letter chips. Full mastheads are too
# long to set as discs, so each outlet gets the word people actually say.
SHORT_NAME = {
    "Express Tribune": "TRIBUNE",
    "Business Recorder": "RECORDER",
    "The Nation": "NATION",
    "Pakistan Observer": "OBSERVER",
    "Minute Mirror": "MIRROR",
    "Daily Times": "TIMES",
    "ARY News": "ARY",
    "BOL News": "BOL",
    "The News": "NEWS",   # no longer collected, but still in the archive
}

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

  /* The scroll palette for story pages. */
  --orange: #ff6d1f;
  --blue:   #5ab0ff;
  --lime:   #c8f02e;

  --serif: "Newsreader", "Times New Roman", Times, serif;
  --sans:  "Archivo", "Helvetica Neue", Helvetica, Arial, sans-serif;
  --mono:  "Space Mono", "Courier New", ui-monospace, monospace;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: var(--serif); font-size: 17px; line-height: 1.45;
}
a { color: inherit; text-decoration: none; }
mark { background: var(--marker); color: var(--ink); padding: 0 .12em; }

.label { font-family: var(--mono); font-size: .62rem; letter-spacing: .13em;
         text-transform: uppercase; color: var(--soft); }

/* ---------------------------------------------------------- masthead (index) */
.masthead { padding: 1.5rem 1.4rem 1rem; border-bottom: 1px solid var(--ink); }
.wordmark {
  display: flex; align-items: center; gap: .08em; line-height: .86;
  font-family: var(--sans); font-weight: 800; letter-spacing: -.035em;
  font-size: clamp(2.8rem, 11vw, 7rem);
}
.wordmark .disc {
  display: inline-flex; align-items: center; justify-content: center;
  width: .92em; height: .92em; border-radius: 50%;
  background: var(--ink); color: var(--paper); margin: 0 .06em;
  font-size: .82em; padding-bottom: .06em;
}
.wordmark .serif { font-family: var(--serif); font-weight: 400; letter-spacing: -.02em; }
.tagline { font-family: var(--sans); font-weight: 600; font-size: .78rem;
           line-height: 1.35; margin-top: .7rem; max-width: 30rem; }
.tagline span { color: var(--soft); }

nav.top { padding: .7rem 1.4rem; border-bottom: 1px solid var(--rule);
          display: flex; gap: 1.4rem; }
nav.top a { font-family: var(--sans); font-weight: 600; font-size: .74rem;
            letter-spacing: .04em; text-transform: uppercase; color: var(--soft); }
nav.top a[aria-current] { color: var(--ink); }
nav.top a:hover { color: var(--ink); }

/* ---------------------------------------------------------- the row table */
.rows { list-style: none; margin: 0; padding: 0; }
.rows > li {
  position: relative; display: grid; align-items: center;
  grid-template-columns: 5rem minmax(0,1fr) minmax(0,15rem) 5.5rem;
  gap: 1.2rem; padding: 1rem 1.4rem;
  border-bottom: 1px solid var(--rule);
  transition: background .12s linear, color .12s linear;
}
.rows > li:hover, .rows > li:focus-within { background: var(--ink); color: var(--paper); }
.rows > li:hover .kicker, .rows > li:hover .count,
.rows > li:focus-within .kicker, .rows > li:focus-within .count { color: var(--paper); }
.rows > li:hover .chip i, .rows > li:focus-within .chip i { border-color: var(--paper); }
.rows > li:hover mark, .rows > li:focus-within mark { background: var(--marker); color: var(--ink); }

.no    { font-family: var(--sans); font-weight: 500; font-size: .72rem; letter-spacing: .06em; }
.kicker{ font-family: var(--serif); font-size: .78rem; color: var(--soft);
         margin-bottom: .12rem; transition: color .12s linear; }
.title { font-family: var(--serif); font-weight: 400; font-size: 1.42rem;
         line-height: 1.14; letter-spacing: -.012em; }
.title a::after { content: ""; position: absolute; inset: 0; }  /* whole row clickable */
.count { font-family: var(--sans); font-weight: 600; font-size: .72rem;
         text-align: right; color: var(--soft); transition: color .12s linear; }

/* circled-letter outlet chips */
.chips { display: flex; flex-wrap: wrap; gap: .3rem .45rem; }
.chip  { display: inline-flex; gap: .06em; }
.chip i {
  display: inline-flex; align-items: center; justify-content: center;
  width: 1.35em; height: 1.35em; border: 1px solid currentColor; border-radius: 50%;
  font-family: var(--sans); font-weight: 600; font-size: .56rem; font-style: normal;
  line-height: 1; letter-spacing: 0; transition: border-color .12s linear;
}
.rows > li.muted { color: var(--soft); }
.rows > li.muted:hover { background: none; color: var(--soft); }

@media (max-width: 860px) {
  .rows > li { grid-template-columns: 3.2rem minmax(0,1fr); gap: .5rem 1rem; }
  .rows > li > .chips, .rows > li > .count { grid-column: 2; }
  .count { text-align: left; }
  .title { font-size: 1.2rem; }
}

/* ---------------------------------------------------------- plain pages */
main.plain { padding: 2rem 1.4rem 5rem; max-width: 54rem; }
main.plain h1 { font-family: var(--serif); font-weight: 400; font-size: 2.4rem;
                line-height: 1.05; letter-spacing: -.02em; margin: 0 0 .5rem; }
main.plain .sub { color: var(--soft); font-size: .95rem; margin: 0 0 2rem; max-width: 42rem; }
.note { margin-top: 2rem; color: var(--soft); font-size: .84rem; max-width: 42rem; }

.bars { list-style: none; padding: 0; margin: 0; border-top: 1px solid var(--rule); }
.bars > li { display: grid; grid-template-columns: 9rem 1fr 3rem; align-items: center;
             gap: .9rem; padding: .5rem 0; }
.bars .w { font-family: var(--sans); font-weight: 700; font-size: .84rem;
           text-transform: uppercase; letter-spacing: .02em; }
.bars .bar { height: .85rem; background: var(--ink); }
.bars .n { font-family: var(--mono); font-size: .78rem; text-align: right; color: var(--soft); }
details { border-bottom: 1px solid var(--rule); padding-bottom: .5rem; }
summary { cursor: pointer; font-family: var(--mono); font-size: .68rem;
          letter-spacing: .04em; color: var(--soft); }
details ul { margin: .5rem 0 0; padding-left: 1.1rem; font-size: .92rem; }
details li { margin-bottom: .25rem; }
details a { border-bottom: 1px solid var(--rule); }

.tabs { margin: 0 0 1.2rem; display: flex; gap: .4rem; }
.tabs button { font: 600 .72rem var(--sans); letter-spacing: .06em; text-transform: uppercase;
               background: none; border: 1px solid var(--ink); padding: .4rem .9rem;
               cursor: pointer; color: var(--ink); }
.tabs button[aria-pressed="true"] { background: var(--ink); color: var(--paper); }
.hidden { display: none; }

table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .65rem .5rem; border-bottom: 1px solid var(--rule); }
th { font: 600 .68rem var(--sans); letter-spacing: .08em; text-transform: uppercase; color: var(--soft); }
td { font-size: .95rem; }
td.num, th.num { text-align: right; font-family: var(--mono); font-size: .85rem; }
th.num { font-family: var(--sans); }
td small { display: block; font: 400 .68rem var(--mono); color: var(--soft); letter-spacing: .04em; }

/* ---------------------------------------------------------- story page */
.doc { color: var(--ink); }
.doc .frame { max-width: 1180px; margin: 0 auto; border-left: 1px solid var(--ink);
              border-right: 1px solid var(--ink); }
/* Content bands stay flat; the colour change happens inside the pull quotes,
   so text never has to sit on the muddy middle of a gradient. */
.band-orange { background: var(--orange); }
.band-blue   { background: var(--blue); }
.band-lime   { background: var(--lime); }
.to-blue { background: linear-gradient(180deg, var(--orange) 0%, var(--blue) 100%); }
.to-lime { background: linear-gradient(180deg, var(--blue) 0%, var(--lime) 100%); }

.topbar { display: flex; align-items: center; justify-content: space-between;
          padding: .8rem 1rem; }
.topbar .pill { font: 400 .6rem var(--mono); letter-spacing: .16em; text-transform: uppercase;
                border: 1px solid var(--ink); border-radius: 999px; padding: .28rem .9rem; }
.topbar .home { font: 400 .6rem var(--mono); letter-spacing: .12em; text-transform: uppercase; }
.topbar .home:hover { text-decoration: underline; }
.topbar .ek { font: 800 1.05rem var(--sans); letter-spacing: -.04em;
              border: 1px solid var(--ink); padding: .12rem .5rem; }

/* the empty cell strip under the nav */
.cells { display: grid; grid-template-columns: repeat(8, 1fr); border-top: 1px solid var(--ink); }
.cells div { height: 2.2rem; border-right: 1px solid var(--ink); }
.cells div:last-child { border-right: 0; }
@media (max-width: 700px) { .cells { grid-template-columns: repeat(4, 1fr); } }

/* hero: artwork slot with the title sitting over it */
.hero { position: relative; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); }
.art { aspect-ratio: 16 / 8.5; background: rgba(255,255,255,.16); }
@media (max-width: 700px) { .art { aspect-ratio: 4 / 5; } }
.hero-title {
  position: absolute; inset: 0; margin: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center;
  font-family: var(--serif); font-weight: 400; letter-spacing: -.035em; line-height: .92;
  font-size: clamp(2.1rem, 7.4vw, 5.6rem); padding: 3.6rem 1rem;
}
.hero-meta { position: absolute; top: 1.4rem; width: 100%; display: flex;
             justify-content: space-between; padding: 0 1.2rem; pointer-events: none; }
.hero-meta div { font: 400 .58rem/1.5 var(--mono); letter-spacing: .12em; text-transform: uppercase; }
.hero-meta .r { text-align: right; }
.hero-foot { position: absolute; bottom: 1.4rem; width: 100%; text-align: center;
             font: 400 .58rem var(--mono); letter-spacing: .16em; text-transform: uppercase; }

/* numbered sections */
.sec { position: relative; border-bottom: 1px solid var(--ink);
       display: grid; grid-template-columns: 6.5rem 1fr 6.5rem; }
.sec .marker { border-right: 1px solid var(--ink); }
.sec .marker b { display: block; width: 1.7rem; margin: .5rem 0 0 .5rem;
                 border: 1px solid var(--ink); text-align: center;
                 font: 400 .55rem/1.5 var(--mono); }
.sec .tail { border-left: 1px solid var(--ink); }
.sec .inner { padding: 2.6rem 2rem; }
@media (max-width: 860px) {
  .sec { grid-template-columns: 2.6rem 1fr; }
  .sec .tail { display: none; }
  .sec .inner { padding: 1.8rem 1.1rem; }
}

.lead { font-family: var(--serif); font-size: clamp(1.25rem, 2.5vw, 1.85rem);
        line-height: 1.28; letter-spacing: -.015em; margin: 0; max-width: 34ch; }
.sec p.body { font-size: 1rem; line-height: 1.6; max-width: 62ch; margin: 1.4rem 0 0; }

/* the pull quote at each colour change, with its ghost behind it */
.quote { position: relative; text-align: center; padding: 4.5rem 1.2rem; border-bottom: 1px solid var(--ink); }
.quote q { display: block; position: relative; quotes: none;
           font: 400 clamp(1.05rem, 3.1vw, 2.1rem)/1.32 var(--mono);
           letter-spacing: -.02em; max-width: 22ch; margin: 0 auto; }
.quote .ghost { position: absolute; inset: 0; display: flex; align-items: center;
                justify-content: center; opacity: .34; transform: translateY(.42em);
                pointer-events: none; }
.quote .ghost span { font: 400 clamp(1.05rem, 3.1vw, 2.1rem)/1.32 var(--mono);
                     letter-spacing: -.02em; max-width: 22ch; text-align: center; }
.quote .who { margin-top: 1.6rem; font: 400 .58rem var(--mono);
              letter-spacing: .16em; text-transform: uppercase; }
/* Narrow columns wrap the quote onto enough lines that the offset ghost starts
   interleaving with the real text, so drop it on small screens. */
@media (max-width: 700px) { .quote .ghost { display: none; } .quote { padding: 3rem 1rem; } }

/* the headline wall */
.wall { list-style: none; margin: 0; padding: 0; }
.wall li { display: grid; grid-template-columns: 11rem 1fr; gap: 1.4rem;
           padding: 1.15rem 0; border-top: 1px solid rgba(18,17,15,.28); }
.wall li:first-child { border-top: 0; }
.wall .who { font-family: var(--mono); font-size: .6rem; line-height: 1.7;
             letter-spacing: .1em; text-transform: uppercase; }
.wall .who .chip { display: flex; gap: .06em; margin-bottom: .45rem; }
.wall .who b { display: block; font-weight: 400; }
.wall .who span { display: block; opacity: .72; }
.wall .hl { font-family: var(--serif); font-size: 1.22rem; line-height: 1.26; letter-spacing: -.01em; }
.wall .hl a:hover { text-decoration: underline; text-underline-offset: .18em; }
@media (max-width: 700px) { .wall li { grid-template-columns: 1fr; gap: .4rem; } }

.docfoot { display: grid; grid-template-columns: 1fr 1fr; }
.docfoot div { padding: 1.6rem 1.2rem; font: 400 .6rem/1.7 var(--mono);
               letter-spacing: .1em; text-transform: uppercase; }
.docfoot div:first-child { border-right: 1px solid var(--ink); }
.docfoot a { border-bottom: 1px solid var(--ink); }
"""


def chip(outlet):
    """An outlet name set as circled letters, the way Off Mute sets its guests."""
    short = SHORT_NAME.get(outlet, outlet).upper()
    letters = "".join(f"<i>{html.escape(c)}</i>" for c in short if c != " ")
    return f'<span class="chip" title="{html.escape(outlet)}">{letters}</span>'


def document(title, body, head_class=""):
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
<body class="{head_class}">
{body}
</body>
</html>"""


def masthead(current, depth=0):
    root = "../" * depth
    items = [("index.html", "Today", "today"),
             ("words.html", "Word watch", "words"),
             ("outlets.html", "Outlets", "outlets")]
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


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------- index

def build_index(stories, built_at):
    rows = []
    for n, s in enumerate(stories, 1):
        chips = "".join(chip(o) for o in dict.fromkeys(i["outlet"] for i in s["items"]))
        kicker = s["section"] or "Story"
        loaded = f' &middot; {plural(s["loaded_count"], "loaded word")}' if s["loaded_count"] else ""
        rows.append(
            f'<li>'
            f'<div class="no">NO {n:02d}</div>'
            f'<div><div class="kicker">{html.escape(kicker)}{loaded}</div>'
            f'<div class="title"><a href="story/{s["id"]}.html">{html.escape(s["title"])}</a></div></div>'
            f'<div class="chips">{chips}</div>'
            f'<div class="count">{s["outlets"]} outlets<br>{len(s["items"])} headlines</div>'
            f'</li>'
        )
    if not rows:
        rows.append('<li class="muted"><div class="no">NO 01</div><div><div class="title">'
                    'No story has been picked up by two outlets yet</div></div></li>')

    days = "24 hours" if INDEX_DAYS == 1 else f"{INDEX_DAYS} days"
    body = f"""{masthead("today")}
<ul class="rows">
{''.join(rows)}
<li class="muted"><div class="no">NO {len(stories) + 1:02d}</div>
<div><div class="kicker">Updated {html.escape(built_at)}</div>
<div class="title">More as the outlets file it &mdash; collected every 30 minutes</div></div></li>
</ul>"""
    return document("Today", body)


# ---------------------------------------------------------------- story

def build_story(s):
    wall = []
    for i in s["items"]:
        wall.append(
            f'<li><div class="who">{chip(i["outlet"])}'
            f'<b>{html.escape(i["outlet"])}</b><span>{fmt_time(i["ts"])}</span></div>'
            f'<div class="hl"><a href="{html.escape(i["url"])}" rel="noopener" target="_blank">'
            f'{highlight(i["title"])}</a></div></li>'
        )

    loudest, plain = s["loudest"], s["plain"]
    has_contrast = loudest["id"] != plain["id"]
    outlets_line = plural(s["outlets"], "outlet")

    lead = (f'{outlets_line} ran this story. Below is every headline, '
            f'word for word, in the order they filed it.')
    if s["loaded_count"]:
        lead += (f' {plural(s["loaded_count"], "word")} from our loaded-language '
                 f'list {"is" if s["loaded_count"] == 1 else "are"} marked.')

    # The closing quote is the plainest telling, set against the loudest one above
    # it. When one outlet happens to be both, there is no contrast to draw, so the
    # band closes on the count instead - the colour run still has to finish.
    if has_contrast:
        closing, closing_who = plain["title"], f'The plainest &mdash; {html.escape(plain["outlet"])}'
    else:
        closing = f'{outlets_line}, one event.'
        closing_who = "Every headline above"

    body = f"""<div class="doc">

<div class="band-orange"><div class="frame">
  <div class="topbar">
    <a class="home" href="../index.html">&larr; All stories</a>
    <span class="pill">{html.escape(s["section"] or "Story")}</span>
    <span class="ek">EK</span>
  </div>
  <div class="cells"><div></div><div></div><div></div><div></div><div></div><div></div><div></div><div></div></div>

  <header class="hero">
    <!-- Artwork slot: drop the Fresco piece in here as <img> or <video>, full-bleed. -->
    <div class="art"></div>
    <div class="hero-meta">
      <div>Covered by<br>{html.escape(outlets_line)}</div>
      <div class="r">Updated<br>{fmt_date(s["last_ts"])}</div>
    </div>
    <h1 class="hero-title">{html.escape(s["title"])}</h1>
    <div class="hero-foot">{plural(len(s["items"]), "headline")}</div>
  </header>

  <section class="sec">
    <div class="marker"><b>01</b></div>
    <div class="inner">
      <p class="lead">{html.escape(lead)}</p>
      <p class="body">The title above is not ours. It is simply the plainest headline in the
      set &mdash; the one reaching for the fewest loaded words &mdash; standing in for the event
      itself.</p>
    </div>
    <div class="tail"></div>
  </section>
</div></div>

<div class="to-blue"><div class="frame">
  <section class="quote">
    <div class="ghost"><span>{html.escape(loudest["title"])}</span></div>
    <q>{html.escape(loudest["title"])}</q>
    <div class="who">{"The loudest &mdash; " if has_contrast else ""}{html.escape(loudest["outlet"])}</div>
  </section>
</div></div>

<div class="band-blue"><div class="frame">
  <section class="sec">
    <div class="marker"><b>02</b></div>
    <div class="inner"><ul class="wall">{''.join(wall)}</ul></div>
    <div class="tail"></div>
  </section>
</div></div>

<div class="to-lime"><div class="frame">
  <section class="quote">
    <div class="ghost"><span>{html.escape(closing)}</span></div>
    <q>{html.escape(closing)}</q>
    <div class="who">{closing_who}</div>
  </section>
</div></div>

<div class="band-lime"><div class="frame">
  <div class="docfoot">
    <div><a href="../index.html">Ek Khabar</a><br>One event, every headline</div>
    <div>Headlines link to the original<br>articles at each outlet</div>
  </div>
</div></div>

</div>"""
    return document(s["title"], body, head_class="story")


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


def build_words(conn):
    week = bars_html(*word_counts(load_headlines_since(conn, 7)))
    month = bars_html(*word_counts(load_headlines_since(conn, 30)))
    body = f"""{masthead("words")}
<main class="plain">
<h1>Word watch</h1>
<p class="sub">The loaded words Pakistani outlets reach for most often. Open a row to see who used it.</p>
<div class="tabs">
  <button aria-pressed="true" onclick="show('week',this)">This week</button>
  <button aria-pressed="false" onclick="show('month',this)">This month</button>
</div>
<section id="week">{week}</section>
<section id="month" class="hidden">{month}</section>
<p class="note">Opinion columns are left out. Counting a word is not the same as calling it
wrong &mdash; sometimes a crisis really is a crisis.</p>
</main>
<script>
function show(id, btn) {{
  for (const s of ['week','month']) document.getElementById(s).classList.toggle('hidden', s !== id);
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-pressed', b === btn);
}}
</script>"""
    return document("Word watch", body)


# ---------------------------------------------------------------- outlets

def build_outlets(conn):
    stats = outlet_stats(load_headlines_since(conn, 30))
    rows = "".join(
        f'<tr><td>{chip(s["outlet"])} {html.escape(s["outlet"])}'
        f'{"<small>too few to rank</small>" if s["thin"] else ""}</td>'
        f'<td class="num">{s["headlines"]}</td>'
        f'<td class="num">{s["loaded"]}</td><td class="num">{s["per_100"]}</td></tr>'
        for s in stats
    )
    body = f"""{masthead("outlets")}
<main class="plain">
<h1>Outlets</h1>
<p class="sub">Last 30 days, news sections only. "Per 100" is loaded words per hundred
headlines, so outlets running very different volumes can still be compared.</p>
<table>
<thead><tr><th>Outlet</th><th class="num">Headlines</th>
<th class="num">Loaded</th><th class="num">Per 100</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="note">This measures word choice in headlines only. It says nothing about whether the
reporting is accurate. Outlets we have fewer than {MIN_HEADLINES} headlines from are shown but
not ranked, because at that sample size a few headlines move the rate by tens of points.</p>
</main>"""
    return document("Outlets", body)


# ---------------------------------------------------------------- main

def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    conn = sqlite3.connect(DB_PATH)
    stories = load_stories(conn)
    built_at = utc_now().astimezone(timezone(timedelta(hours=5))).strftime("%d %b %Y, %H:%M PKT")

    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    write(f"{OUT_DIR}/index.html", build_index(stories, built_at))
    for s in stories:
        write(f"{OUT_DIR}/story/{s['id']}.html", build_story(s))
    write(f"{OUT_DIR}/words.html", build_words(conn))
    write(f"{OUT_DIR}/outlets.html", build_outlets(conn))
    write(f"{OUT_DIR}/.nojekyll", "")
    conn.close()
    print(f"Built {len(stories)} story pages into {OUT_DIR}/")


if __name__ == "__main__":
    main()