import requests, feedparser
candidates = {
    "The News latest":   "https://www.thenews.com.pk/rss/1/1",
    "The News national": "https://www.thenews.com.pk/rss/1/8",
    "The News top":      "https://www.thenews.com.pk/rss/2/1",
    "Geo latest":        "https://www.geo.tv/rss/1/1",
    "Geo pakistan":      "https://www.geo.tv/rss/1/8",
    "Geo top":           "https://www.geo.tv/rss/2/1",
}
for name, url in candidates.items():
    try:
        r = requests.get(url, headers={"User-Agent": "EkKhabarBot/0.1"}, timeout=15)
        f = feedparser.parse(r.content)
        newest = f.entries[0].get("published", "?") if f.entries else "-"
        print(f"{name:<20} {r.status_code}  {len(f.entries):>3} items  newest: {newest}")
    except Exception as e:
        print(f"{name:<20} FAILED {e}")