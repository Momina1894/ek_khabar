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
MIN_OUTLETS = 2  # a story needs this many outlets to be shown

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


def load_stories(conn):
    rows = conn.execute("""
        SELECT s.story_id, h.id, h.outlet, h.title, h.url,
               COALESCE(h.published, h.fetched_at) AS ts
        FROM stories s JOIN headlines h ON h.id = s.headline_id
        ORDER BY ts
    """).fetchall()
    stories = defaultdict(list)
    for sid, hid, outlet, title, url, ts in rows:
        stories[sid].append({"id": hid, "outlet": outlet, "title": title, "url": url, "ts": ts})

    result = []
    for sid, items in stories.items():
        outlets = {i["outlet"] for i in items}
        if len(outlets) < MIN_OUTLETS:
            continue
        # The "plainest" headline stands in as the story title: fewest loaded words, then shortest.
        plain = min(items, key=lambda i: (len(find_loaded(i["title"])), len(i["title"])))
        result.append({
            "id": sid,
            "title": plain["title"],
            "items": items,
            "outlets": len(outlets),
            "loaded_count": sum(len(find_loaded(i["title"])) for i in items),
            "first_ts": items[0]["ts"],
        })
    result.sort(key=lambda s: (-s["outlets"], -len(s["items"])))
    return result


def load_headlines_since(conn, days):
    since = (utc_now() - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT outlet, title, url FROM headlines WHERE COALESCE(published, fetched_at) >= ?",
        (since,),
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
    return [
        {"outlet": o, "headlines": total[o], "loaded": loaded[o],
         "per_100": round(100 * loaded[o] / total[o], 1) if total[o] else 0}
        for o in sorted(total, key=lambda o: -(loaded[o] / total[o] if total[o] else 0))
    ]


# ---------------------------------------------------------------- html

CSS = """
:root {
  --ink: #1c1a17;
  --ink-soft: #6b665e;
  --paper: #fbfaf7;
  --rule: #e4e0d8;
  --marker: #ffe86b;
  --link: #1b4d8c;
}
* { box-sizing: border-box; }
html { font-size: 17px; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: Georgia, "Times New Roman", serif; line-height: 1.5;
}
a { color: var(--link); text-decoration: none; }
a:hover, a:focus-visible { text-decoration: underline; }
mark { background: var(--marker); color: inherit; padding: 0 .15em; border-radius: .15em; }

header { padding: 1.4rem 1.2rem .9rem; border-bottom: 1px solid var(--rule); }
header .brand { font-size: 1.6rem; font-weight: 700; letter-spacing: -.01em; color: var(--ink); }
header .brand span { color: var(--ink-soft); font-weight: 400; font-style: italic; margin-left: .6rem; font-size: 1rem; }
nav { margin-top: .5rem; font-size: .95rem; }
nav a { margin-right: 1.2rem; color: var(--ink-soft); }
nav a[aria-current] { color: var(--ink); border-bottom: 2px solid var(--marker); }

main { max-width: 760px; margin: 0 auto; padding: 1.6rem 1.2rem 4rem; }
h1 { font-size: 1.9rem; line-height: 1.2; margin: 0 0 .4rem; letter-spacing: -.01em; }
h2 { font-size: 1.25rem; margin: 2.2rem 0 .8rem; }
.sub { color: var(--ink-soft); margin: 0 0 1.6rem; }

/* index: story list */
.story-list { list-style: none; padding: 0; margin: 0; }
.story-list li { padding: 1rem 0; border-top: 1px solid var(--rule); }
.story-list li:last-child { border-bottom: 1px solid var(--rule); }
.story-list a.t { font-size: 1.2rem; color: var(--ink); display: block; line-height: 1.3; }
.story-list .meta { color: var(--ink-soft); font-size: .9rem; margin-top: .3rem; }

/* story page: headline wall */
.wall { list-style: none; padding: 0; margin: 1rem 0 0; border-top: 1px solid var(--rule); }
.wall li { display: grid; grid-template-columns: 8.5rem 1fr; gap: 1rem; padding: .9rem 0; border-bottom: 1px solid var(--rule); }
.wall .who { color: var(--ink-soft); font-size: .9rem; padding-top: .15rem; }
.wall .who small { display: block; font-size: .8rem; }
.wall .hl { font-size: 1.15rem; line-height: 1.35; color: var(--ink); }
.wall .hl a { color: inherit; }
.note { margin-top: 1.6rem; color: var(--ink-soft); font-size: .9rem; }
@media (max-width: 520px) {
  .wall li { grid-template-columns: 1fr; gap: .2rem; }
}

/* word watch bars */
.bars { list-style: none; padding: 0; margin: 0; }
.bars li { display: grid; grid-template-columns: 8rem 1fr 3rem; align-items: center; gap: .8rem; padding: .35rem 0; }
.bars .w { font-weight: 700; }
.bars .bar { height: 1.1rem; background: var(--marker); border-radius: .15rem; }
.bars .n { color: var(--ink-soft); text-align: right; font-size: .9rem; }
details { margin: .2rem 0 .6rem; }
summary { cursor: pointer; color: var(--ink-soft); font-size: .9rem; }
details ul { margin: .4rem 0 0; padding-left: 1.2rem; font-size: .95rem; }
.tabs { margin: 0 0 1rem; font-size: .95rem; }
.tabs button { font: inherit; background: none; border: 1px solid var(--rule); padding: .3rem .8rem; cursor: pointer; color: var(--ink-soft); border-radius: .3rem; margin-right: .4rem; }
.tabs button[aria-pressed="true"] { background: var(--marker); color: var(--ink); border-color: var(--marker); }
.hidden { display: none; }

/* outlets table */
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .6rem .4rem; border-bottom: 1px solid var(--rule); }
th { font-weight: 400; color: var(--ink-soft); font-size: .9rem; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
th.num { text-align: right; }
"""


def page(title, body, current, depth=0):
    root = "../" * depth
    nav_items = [("index.html", "Today", "today"), ("words.html", "Word watch", "words"), ("outlets.html", "Outlets", "outlets")]
    nav = "".join(
        f'<a href="{root}{href}"{" aria-current=\"page\"" if key == current else ""}>{label}</a>'
        for href, label, key in nav_items
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - {SITE_NAME}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <a class="brand" href="{root}index.html">{SITE_NAME}<span>one event, every headline</span></a>
  <nav>{nav}</nav>
</header>
<main>
{body}
</main>
</body>
</html>"""


def fmt_time(ts):
    try:
        dt = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=5)))  # PKT
        return dt.strftime("%d %b, %H:%M")
    except Exception:
        return ""


def build_index(stories, built_at):
    items = []
    for s in stories:
        items.append(
            f'<li><a class="t" href="story/{s["id"]}.html">{html.escape(s["title"])}</a>'
            f'<div class="meta">{s["outlets"]} outlets, {len(s["items"])} headlines'
            f'{", " + str(s["loaded_count"]) + " loaded words" if s["loaded_count"] else ""}</div></li>'
        )
    if not items:
        items.append("<li>No stories with more than one outlet yet. Check back after a few collection runs.</li>")
    body = f"""<h1>Today's stories</h1>
<p class="sub">Stories covered by {MIN_OUTLETS}+ Pakistani outlets in the last two days. Updated {built_at}.</p>
<ul class="story-list">{''.join(items)}</ul>"""
    return page("Today", body, "today")


def build_story(s):
    rows = []
    for i in s["items"]:
        rows.append(
            f'<li><div class="who">{html.escape(i["outlet"])}<small>{fmt_time(i["ts"])}</small></div>'
            f'<div class="hl"><a href="{html.escape(i["url"])}" rel="noopener">{highlight(i["title"])}</a></div></li>'
        )
    body = f"""<h1>{html.escape(s["title"])}</h1>
<p class="sub">{s["outlets"]} outlets, {len(s["items"])} headlines. Highlighted words are from our loaded-language list.</p>
<ul class="wall">{''.join(rows)}</ul>
<p class="note">Headlines link to the original articles. The story title above is simply the plainest headline in the set, not our own summary.</p>"""
    return page(s["title"], body, "today", depth=1)


def bars_html(counts, by_outlet, examples, limit=25):
    top = counts.most_common(limit)
    if not top:
        return "<p>No loaded words found in this period.</p>"
    mx = top[0][1]
    out = ['<ul class="bars">']
    for w, n in top:
        width = max(2, round(100 * n / mx))
        who = ", ".join(f"{o} {c}" for o, c in by_outlet[w].most_common())
        ex = "".join(
            f'<li><a href="{html.escape(u)}">{highlight(t)}</a> <em>({html.escape(o)})</em></li>'
            for o, t, u in examples[w]
        )
        out.append(
            f'<li><span class="w">{html.escape(w)}</span><div class="bar" style="width:{width}%"></div><span class="n">{n}</span></li>'
            f'<details><summary>{html.escape(who)}</summary><ul>{ex}</ul></details>'
        )
    out.append("</ul>")
    return "".join(out)


def build_words(conn):
    week = bars_html(*word_counts(load_headlines_since(conn, 7)))
    month = bars_html(*word_counts(load_headlines_since(conn, 30)))
    body = f"""<h1>Word watch</h1>
<p class="sub">Loaded words used most often in headlines. Click a row to see who used it.</p>
<div class="tabs">
  <button aria-pressed="true" onclick="show('week',this)">This week</button>
  <button aria-pressed="false" onclick="show('month',this)">This month</button>
</div>
<section id="week">{week}</section>
<section id="month" class="hidden">{month}</section>
<script>
function show(id, btn) {{
  for (const s of ['week','month']) document.getElementById(s).classList.toggle('hidden', s !== id);
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-pressed', b === btn);
}}
</script>"""
    return page("Word watch", body, "words")


def build_outlets(conn):
    stats = outlet_stats(load_headlines_since(conn, 30))
    rows = "".join(
        f'<tr><td>{html.escape(s["outlet"])}</td><td class="num">{s["headlines"]}</td>'
        f'<td class="num">{s["loaded"]}</td><td class="num">{s["per_100"]}</td></tr>'
        for s in stats
    )
    body = f"""<h1>Outlets</h1>
<p class="sub">Last 30 days. "Per 100" is loaded words per hundred headlines, so outlets with different volumes can be compared.</p>
<table>
<thead><tr><th>Outlet</th><th class="num">Headlines</th><th class="num">Loaded words</th><th class="num">Per 100</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="note">This measures word choice in headlines only. It says nothing about whether the reporting is accurate.</p>"""
    return page("Outlets", body, "outlets")


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
