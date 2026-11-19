import inspect
import unittest
from unittest.mock import patch

import json
from unittest.mock import MagicMock

from src.github_api import (
    MAX_JOB_PAGES,
    create_github_api_client,
    list_all_jobs_for_workflow_run,
)


class TestCreateGithubApiClient(unittest.TestCase):
    @patch("src.github_api.GhApi")
    def test_uses_synchronous_operations(self, gh_api):
        create_github_api_client(owner="example", repo="repository", token="token")

        gh_api.assert_called_once_with(
            owner="example",
            repo="repository",
            token="token",
            sync=True,
        )

    def test_operations_are_synchronous(self):
        api = create_github_api_client(
            owner="example",
            repo="repository",
            token="token",
        )

        self.assertFalse(
            inspect.iscoroutinefunction(api.actions.get_workflow_run.__call__)
        )


def fake_api(pages, total_count):
    """An api whose list_jobs_for_workflow_run returns `pages` (lists of jobs) in order."""
    api = MagicMock()

    def list_jobs(run_id, page, per_page):
        jobs = pages[page - 1] if page <= len(pages) else []
        return {"total_count": total_count, "jobs": jobs}

    api.actions.list_jobs_for_workflow_run.side_effect = list_jobs
    return api


def jobs(start, count):
    return [{"id": i, "name": "job {}".format(i)} for i in range(start, start + count)]


class TestListAllJobsForWorkflowRun(unittest.TestCase):
    def test_single_page(self):
        api = fake_api([jobs(0, 40)], total_count=40)

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps)

        self.assertEqual(len(result["jobs"]), 40)
        self.assertEqual(result["total_count"], 40)
        api.actions.list_jobs_for_workflow_run.assert_called_once_with(1, page=1, per_page=100)

    def test_follows_pages_past_100_jobs(self):
        api = fake_api([jobs(0, 100), jobs(100, 100), jobs(200, 8)], total_count=208)

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps)

        self.assertEqual([j["id"] for j in result["jobs"]], list(range(208)))
        self.assertEqual(api.actions.list_jobs_for_workflow_run.call_count, 3)

    def test_exactly_one_full_page_stops_without_extra_call(self):
        api = fake_api([jobs(0, 100)], total_count=100)

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps)

        self.assertEqual(len(result["jobs"]), 100)
        self.assertEqual(api.actions.list_jobs_for_workflow_run.call_count, 1)

    def test_stops_on_empty_page_when_total_count_overstates(self):
        api = fake_api([jobs(0, 100)], total_count=150)

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps)

        self.assertEqual(len(result["jobs"]), 100)
        self.assertEqual(api.actions.list_jobs_for_workflow_run.call_count, 2)

    def test_page_cap_stops_a_runaway_api(self):
        api = MagicMock()
        api.actions.list_jobs_for_workflow_run.side_effect = (
            lambda run_id, page, per_page: {"total_count": 10**9, "jobs": jobs(page * 100, 100)}
        )

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps)

        self.assertEqual(api.actions.list_jobs_for_workflow_run.call_count, MAX_JOB_PAGES)
        self.assertEqual(len(result["jobs"]), MAX_JOB_PAGES * 100)


if __name__ == "__main__":
    unittest.main()
