"""
Ek Khabar - loaded_words.txt auditor

The word list is the heart of Word Watch, and it is hand-written, so it is worth
checking against the corpus rather than trusting it. This reports what each word
actually matched.

Two failure modes it is looking for:

  dead words     in the list but never used, so they only add noise to the regex
  false friends  matching a literal use rather than a loaded one. "Tanks" is the
                 clearest case: the list means a market that tanked, but Pakistani
                 headlines mostly mean the vehicle.

It does not decide anything. It prints the evidence so a person can.

Usage:
    python audit_words.py                 # summary, plus dead words
    python audit_words.py --samples       # every live word with example headlines
    python audit_words.py --word tanks    # every match for one word
"""

import sqlite3
import sys
from collections import Counter, defaultdict

from build import LOADED, SKIP_SECTIONS, find_loaded

DB_PATH = "headlines.db"


def corpus(conn):
    return conn.execute(
        "SELECT outlet, title FROM headlines "
        "WHERE section IS NULL OR section NOT IN (%s)"
        % ",".join("?" * len(SKIP_SECTIONS)), SKIP_SECTIONS
    ).fetchall()


def tally(rows):
    counts, examples, outlets = Counter(), defaultdict(list), defaultdict(Counter)
    for outlet, title in rows:
        for w in set(find_loaded(title)):
            base = next((b for b in LOADED if w.startswith(b)), w)
            counts[base] += 1
            outlets[base][outlet] += 1
            if len(examples[base]) < 40:
                examples[base].append((outlet, title))
    return counts, examples, outlets


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = corpus(conn)
    counts, examples, outlets = tally(rows)

    if "--word" in sys.argv:
        want = sys.argv[sys.argv.index("--word") + 1].lower()
        print(f'"{want}" matched {counts.get(want, 0)} headlines\n')
        for outlet, title in examples.get(want, []):
            print(f"  [{outlet}] {title}")
        return

    dead = [w for w in LOADED if not counts.get(w)]
    live = counts.most_common()

    print(f"{len(rows)} headlines, {len(LOADED)} words in the list, "
          f"{len(live)} of them used, {len(dead)} never used.\n")

    print(f"{'word':<18}{'hits':>6}   spread")
    print("-" * 70)
    for w, n in live:
        spread = len(outlets[w])
        print(f"{w:<18}{n:>6}   {spread} outlet{'' if spread == 1 else 's'}")

    if dead:
        print(f"\nNever matched ({len(dead)}) - dead weight in the pattern:")
        print("  " + ", ".join(sorted(dead)))

    if "--samples" in sys.argv:
        print("\n" + "=" * 70)
        print("Examples, for judging whether the match is loaded or literal")
        print("=" * 70)
        for w, n in live:
            print(f"\n--- {w} ({n}) ---")
            for outlet, title in examples[w][:4]:
                print(f"  [{outlet}] {title}")


if __name__ == "__main__":
    main()
