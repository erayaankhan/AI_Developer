"""Tests for deploy_staging_local.AssemblaClient milestone lookup.

Run: python -m unittest AI_DEV_ENV/python_scripts/test_deploy_staging_local.py -v
(from the repo root, or `python -m unittest test_deploy_staging_local -v` from this folder).

Regression for the "No milestone with '_CURRENT' ... nothing to process this cycle" failure:
Assembla returns only 10 milestones per request, so an unpaged lookup never saw the _CURRENT
milestone once the space had more than 10.
"""
import re
import unittest
from unittest import mock

import deploy_staging_local as d


def milestone(i, title=None):
    return {"id": i, "title": title or f"Milestone {i}"}


class FindCurrentMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.client = d.AssemblaClient("key", "secret")

    def _serve(self, pages):
        """Patch _get_json to serve the given pages (list of lists); record the requested URLs."""
        urls = []

        def fake_get_json(url, *args, **kwargs):
            urls.append(url)
            # "per_page=100&page=2" contains "page=" twice, so match the standalone parameter.
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return pages[page - 1] if page <= len(pages) else []

        patcher = mock.patch.object(self.client, "_get_json", side_effect=fake_get_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        return urls

    def test_finds_current_milestone_beyond_the_first_page(self):
        first = [milestone(i) for i in range(100)]
        second = [milestone(100), milestone(101, "Workflow 3.17.0_CURRENT")]
        urls = self._serve([first, second])

        found = self.client._find_current_milestone("https://x/milestones.json")

        self.assertEqual(found["id"], 101)
        self.assertEqual(len(urls), 2)

    def test_stops_after_the_first_short_page_when_there_is_no_current_milestone(self):
        urls = self._serve([[milestone(i) for i in range(100)], [milestone(100)]])

        self.assertIsNone(self.client._find_current_milestone("https://x/milestones.json"))
        self.assertEqual(len(urls), 2)

    def test_returns_the_first_match_without_fetching_more_pages(self):
        first = [milestone(1, "Workflow 3.17.0_CURRENT")] + [milestone(i) for i in range(2, 101)]
        urls = self._serve([first, [milestone(200)]])

        found = self.client._find_current_milestone("https://x/milestones.json")

        self.assertEqual(found["id"], 1)
        self.assertEqual(len(urls), 1)

    def test_requests_100_per_page_and_numbers_the_pages(self):
        urls = self._serve([[milestone(i) for i in range(100)], []])

        self.client._find_current_milestone("https://x/milestones.json")

        self.assertEqual(urls[0], "https://x/milestones.json?per_page=100&page=1")
        self.assertEqual(urls[1], "https://x/milestones.json?per_page=100&page=2")

    def test_an_empty_or_missing_response_means_no_milestone(self):
        with mock.patch.object(self.client, "_get_json", return_value=None):
            self.assertIsNone(self.client._find_current_milestone("https://x/milestones.json"))

    def test_gives_up_after_max_pages(self):
        full = [milestone(i) for i in range(100)]
        with mock.patch.object(self.client, "_get_json", return_value=full) as get_json:
            self.assertIsNone(self.client._find_current_milestone("https://x/m.json", max_pages=3))
        self.assertEqual(get_json.call_count, 3)

    def test_resolve_falls_back_to_the_full_list(self):
        current = milestone(7, "Workflow 3.17.0_CURRENT")

        def fake_find(url, *args, **kwargs):
            return current if url.endswith("/milestones/all.json") else None

        with mock.patch.object(self.client, "_find_current_milestone", side_effect=fake_find):
            self.assertEqual(self.client.resolve_current_milestone()["id"], 7)


if __name__ == "__main__":
    unittest.main()
