"""
Tests for the parts of Ek Khabar that can go wrong quietly.

Weighted towards bugs this project has actually had:
  - story ids were the clustering library's own labels, which restart at 0 every
    run, so unrelated events from different runs merged into one story
  - one feed clips its headlines, and a clipped headline was standing in as a
    story's title
  - a mixed feed guessing a section pushed opinion columns into the clustering

Run:  python -m unittest discover -v
"""

import sqlite3
import unittest

import build
import cluster
import collector


class LoadedWords(unittest.TestCase):
    def test_matches_a_listed_word(self):
        self.assertIn("crisis", build.find_loaded("Govt faces crisis over budget"))

    def test_matches_plural_and_tense(self):
        self.assertIn("warns", build.find_loaded("PM warns of shutdown"))
        self.assertIn("vows", build.find_loaded("Minister vows action"))

    def test_ignores_words_inside_other_words(self):
        # "hike" must not fire on "hiker"
        self.assertEqual(build.find_loaded("Lost hiker found in Murree"), [])

    def test_highlight_marks_the_word(self):
        self.assertIn("<mark>crisis</mark>", build.highlight("A crisis unfolds"))

    def test_highlight_escapes_html(self):
        out = build.highlight('Ministry <b>"quoted"</b> and cited a crisis')
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("<mark>crisis</mark>", out)

    def test_literal_words_are_not_on_the_list(self):
        # Audited out: these matched literal uses, not loaded ones.
        for w in ("attacks", "bomb", "tanks", "crashes", "plot", "failed"):
            self.assertNotIn(w, build.LOADED, "{0} is back on the list".format(w))


class ClippedHeadlines(unittest.TestCase):
    def test_detects_both_ellipsis_forms(self):
        self.assertTrue(build.looks_clipped("Nepal flood toll hits 939 as rescue ..."))
        self.assertTrue(build.looks_clipped("Nepal flood toll hits 939 as rescue…"))
        self.assertTrue(collector.looks_truncated("something ..."))

    def test_complete_headline_is_not_clipped(self):
        self.assertFalse(build.looks_clipped("Nepal flood toll hits 939 as rescue continues"))
        self.assertFalse(collector.looks_truncated("A complete headline"))


class Sections(unittest.TestCase):
    def test_entry_tag_beats_the_feed_default(self):
        entry = {"tags": [{"term": "Sports"}]}
        self.assertEqual(collector.entry_section(entry, "Pakistan"), "Sports")

    def test_packed_tags_are_split(self):
        entry = {"tags": [{"term": "Must Read, Pakistan"}]}
        self.assertEqual(collector.entry_section(entry, None), "Pakistan")

    def test_prominence_labels_are_not_sections(self):
        entry = {"tags": [{"term": "Top News"}]}
        self.assertIsNone(collector.entry_section(entry, None))

    def test_mixed_feed_does_not_guess(self):
        # A feed carrying several desks must leave section unset rather than
        # guess, or Dawn's opinion columns get labelled Pakistan and clustered.
        self.assertIsNone(collector.entry_section({"tags": []}, None))

    def test_editorials_map_to_opinion(self):
        self.assertEqual(collector.normalise_section("Editorials"), "Opinion")


class StoryIds(unittest.TestCase):
    """The bug that silently merged around 40% of stories."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        cluster.init_db(self.conn)

    def seed(self, mapping):
        self.conn.executemany(
            "INSERT INTO stories (headline_id, story_id, clustered_at) VALUES (?,?,'t0')",
            list(mapping.items()))

    def test_new_groups_get_fresh_ids(self):
        got = cluster.assign_story_ids(self.conn, {0: [1, 2], 1: [3]})
        self.assertEqual(len(set(got.values())), 2)

    def test_a_continuing_story_keeps_its_id(self):
        self.seed({1: 7, 2: 7})
        got = cluster.assign_story_ids(self.conn, {0: [1, 2, 3]})
        self.assertEqual(got[0], 7, "a group of the same headlines must keep story 7")

    def test_a_split_story_gives_the_id_to_the_larger_half(self):
        self.seed({1: 7, 2: 7, 3: 7})
        got = cluster.assign_story_ids(self.conn, {0: [1, 2], 1: [3]})
        self.assertEqual(got[0], 7)
        self.assertNotEqual(got[1], 7)

    def test_ids_are_never_reused(self):
        self.seed({1: 7, 2: 7})
        fresh = cluster.assign_story_ids(self.conn, {0: [90], 1: [91]})
        self.assertTrue(all(v > 7 for v in fresh.values()),
                        "a new story must not take an id already handed out")

    def test_two_groups_never_share_an_id(self):
        self.seed({1: 7, 2: 7, 3: 7, 4: 7})
        got = cluster.assign_story_ids(self.conn, {0: [1], 1: [2], 2: [3], 3: [4]})
        self.assertEqual(len(set(got.values())), 4)


class Coherence(unittest.TestCase):
    def test_shared_keyword_holds_a_group_together(self):
        self.assertTrue(cluster.coherent([
            "Senate passes finance bill",
            "Finance bill passes Senate vote",
        ]))

    def test_unrelated_headlines_are_rejected(self):
        self.assertFalse(cluster.coherent([
            "Senate passes finance bill",
            "Messi announces retirement",
        ]))


class Palette(unittest.TestCase):
    def test_hue_gap_wraps_around_the_wheel(self):
        self.assertEqual(build.hue_gap(350, 10), 20)

    def test_the_days_colours_are_spread_out(self):
        picked = build.day_colours()
        self.assertEqual(len(picked), 4)
        self.assertEqual(len(set(picked)), 4)
        hues = dict(build.PALETTE)
        for i in range(len(picked)):
            for j in range(i + 1, len(picked)):
                self.assertGreaterEqual(
                    build.hue_gap(hues[picked[i]], hues[picked[j]]), build.MIN_HUE_GAP)


class Plurals(unittest.TestCase):
    def test_singular_and_plural(self):
        self.assertEqual(build.plural(1, "outlet"), "1 outlet")
        self.assertEqual(build.plural(3, "outlet"), "3 outlets")

    def test_irregular_plural(self):
        self.assertEqual(build.plural(2, "story", "stories"), "2 stories")


class StoryAssembly(unittest.TestCase):
    """load_stories against a real (in-memory) database."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        collector.init_db(self.conn)
        cluster.init_db(self.conn)
        now = build.utc_now().isoformat()
        rows = [
            ("Dawn", "Security forces kill 12 militants in Kalat", "u1"),
            ("Geo", "Security forces kill 12 India-backed militants ...", "u2"),
            ("ARY News", "12 militants killed in Kalat operation", "u3"),
        ]
        for outlet, title, url in rows:
            self.conn.execute(
                "INSERT INTO headlines (outlet,title,url,published,fetched_at,section)"
                " VALUES (?,?,?,?,?,'Pakistan')", (outlet, title, url, now, now))
        self.conn.executemany(
            "INSERT INTO stories (headline_id, story_id, clustered_at) VALUES (?,1,'t0')",
            [(1,), (2,), (3,)])
        self.conn.commit()

    def test_groups_headlines_into_one_story(self):
        stories = build.load_stories(self.conn)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["outlets"], 3)

    def test_a_clipped_headline_never_becomes_the_title(self):
        title = build.load_stories(self.conn)[0]["title"]
        self.assertFalse(build.looks_clipped(title),
                         "clipped title chosen: {0}".format(title))

    def test_coverage_gap_names_who_sat_it_out(self):
        # Dawn/Geo/ARY ran it; The Nation was filing to Pakistan but did not.
        now = build.utc_now().isoformat()
        for i in range(build.GAP_MIN_SECTION_HEADLINES):
            self.conn.execute(
                "INSERT INTO headlines (outlet,title,url,published,fetched_at,section)"
                " VALUES ('The Nation',?,?,?,?,'Pakistan')",
                ("Unrelated Nation story {0}".format(i), "n{0}".format(i), now, now))
        self.conn.commit()
        filing = build.outlets_filing_by_section(self.conn)
        self.assertIn("The Nation", filing["Pakistan"])
        story = build.load_stories(self.conn, filing=filing)[0]
        self.assertEqual(story["missing"], ["The Nation"])

    def test_no_gap_claimed_for_a_thinly_covered_story(self):
        # Absence only means something once enough outlets ran it.
        self.conn.execute("DELETE FROM stories WHERE headline_id = 3")
        self.conn.commit()
        filing = {"Pakistan": {"Dawn", "Geo", "ARY News", "The Nation"}}
        story = build.load_stories(self.conn, filing=filing)[0]
        self.assertEqual(story["missing"], [])


if __name__ == "__main__":
    unittest.main()
