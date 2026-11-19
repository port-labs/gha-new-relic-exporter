import inspect
import unittest
from unittest.mock import patch

import json
from unittest.mock import MagicMock

from src.github_api import (
    API_ATTEMPTS,
    JOBS_PER_PAGE,
    MIN_JOBS_PER_PAGE,
    MAX_JOB_PAGES,
    call_with_retries,
    create_github_api_client,
    is_retryable,
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
        api.actions.list_jobs_for_workflow_run.assert_called_once_with(1, page=1, per_page=JOBS_PER_PAGE)

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


class FakeAPIError(Exception):
    def __init__(self, status_code, retryable=False):
        super().__init__("HTTP {}".format(status_code))
        self.status_code = status_code
        self.retryable = retryable


class TestRetries(unittest.TestCase):
    def test_is_retryable(self):
        self.assertTrue(is_retryable(FakeAPIError(502)))
        self.assertTrue(is_retryable(FakeAPIError(503)))
        self.assertTrue(is_retryable(FakeAPIError(429)))
        self.assertTrue(is_retryable(FakeAPIError(400, retryable=True)))
        self.assertFalse(is_retryable(FakeAPIError(404)))
        self.assertFalse(is_retryable(ValueError("boom")))

    def test_retries_transient_error_then_succeeds(self):
        fn = MagicMock(side_effect=[FakeAPIError(502), FakeAPIError(503), "ok"])
        sleeps = []

        self.assertEqual(call_with_retries(fn, 1, page=2, sleep=sleeps.append), "ok")
        self.assertEqual(fn.call_count, 3)
        fn.assert_called_with(1, page=2)
        self.assertEqual(sleeps, [2, 4])

    def test_does_not_retry_client_errors(self):
        fn = MagicMock(side_effect=FakeAPIError(404))

        with self.assertRaises(FakeAPIError):
            call_with_retries(fn, sleep=lambda _: None)
        self.assertEqual(fn.call_count, 1)

    def test_gives_up_after_all_attempts(self):
        fn = MagicMock(side_effect=FakeAPIError(502))

        with self.assertRaises(FakeAPIError):
            call_with_retries(fn, sleep=lambda _: None)
        self.assertEqual(fn.call_count, API_ATTEMPTS)

    def test_job_pages_survive_a_transient_error(self):
        pages = {1: jobs(0, 100), 2: jobs(100, 82)}
        calls = []

        def list_jobs(run_id, page, per_page):
            calls.append(page)
            if page == 2 and calls.count(2) == 1:
                raise FakeAPIError(502)
            return {"total_count": 182, "jobs": pages[page]}

        api = MagicMock()
        api.actions.list_jobs_for_workflow_run.side_effect = list_jobs

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps, sleep=lambda _: None)

        self.assertEqual(len(result["jobs"]), 182)
        self.assertEqual(calls, [1, 2, 2])

    def test_halves_page_size_when_a_page_keeps_failing(self):
        all_jobs = jobs(0, 182)
        sizes = []

        def list_jobs(run_id, page, per_page):
            sizes.append(per_page)
            if per_page > 25:
                raise FakeAPIError(502)
            start = (page - 1) * per_page
            return {"total_count": 182, "jobs": all_jobs[start:start + per_page]}

        api = MagicMock()
        api.actions.list_jobs_for_workflow_run.side_effect = list_jobs

        result = list_all_jobs_for_workflow_run(api, 1, json.dumps, per_page=100, sleep=lambda _: None)

        self.assertEqual([j["id"] for j in result["jobs"]], list(range(182)))
        self.assertEqual(sorted(set(sizes), reverse=True), [100, 50, 25])

    def test_gives_up_at_the_minimum_page_size(self):
        api = MagicMock()
        api.actions.list_jobs_for_workflow_run.side_effect = FakeAPIError(502)

        with self.assertRaises(FakeAPIError):
            list_all_jobs_for_workflow_run(api, 1, json.dumps, sleep=lambda _: None)
        sizes = {c.kwargs["per_page"] for c in api.actions.list_jobs_for_workflow_run.call_args_list}
        self.assertEqual(min(sizes), MIN_JOBS_PER_PAGE)


if __name__ == "__main__":
    unittest.main()
