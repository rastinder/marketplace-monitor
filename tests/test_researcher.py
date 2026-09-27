import json
import pathlib
import unittest

import researcher

ROOT = pathlib.Path(__file__).parent.parent


class TestResearcher(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(ROOT) / "data" / "test-tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        for stale in ("observations.jsonl", "listings.csv", "log.txt"):
            (self.tmp / stale).unlink(missing_ok=True)
        self.rec = researcher.Researcher(self.tmp, self.tmp / "log.txt")

    def _candidate(self, term):
        cand = researcher.Candidate(term=term, title="title", price="CAD 50",
                                     location="Riverton", distance="5 km")
        return cand

    def test_recording_layer(self):
        for term in ("term1", "term2"):
            self.rec.start_search(term)
            self.rec.record_item(self._candidate(term), self.tmp)
            self.rec.end_search(term, 1)
        summary = self.rec.summary()
        self.assertEqual(summary.get("searches_done"), 2)
        # record_item appends one JSON row per observed listing
        rows = [json.loads(line) for line in (self.tmp / "observations.jsonl").read_text().splitlines()]
        self.assertEqual([r["term"] for r in rows], ["term1", "term2"])
        self.assertTrue((self.tmp / "listings.csv").exists())

    def test_summary(self):
        self.rec.start_search("term")
        self.rec.end_search("term", 1)
        summary = self.rec.summary()
        self.assertEqual(summary.get("searches_done"), 1)
        self.assertEqual(summary.get("distinct_terms_tried"), 1)
        self.assertIn("term", summary.get("terms_tried", []))


if __name__ == "__main__":
    unittest.main()
