import json
import time

from ghapi.all import GhApi

# Jobs per page. GitHub allows 100, but on big runs (each job carries its steps) a
# 100-job page often 502s every time; 50 does not. If a page still fails, the page
# size is halved, down to MIN_JOBS_PER_PAGE.
JOBS_PER_PAGE = 50
MIN_JOBS_PER_PAGE = 10
# Safety stop for a misbehaving API: 100 pages is 10,000 jobs, far above a real run.
MAX_JOB_PAGES = 100
# Attempts per GitHub API call; waits 2, 4, 8 s between them.
API_ATTEMPTS = 4


def create_github_api_client(owner, repo, token):
    return GhApi(owner=owner, repo=repo, token=str(token), sync=True)


def _status_code(error):
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(error):
    """GitHub 5xx and 429 are transient; so is anything the client marks retryable."""
    if getattr(error, "retryable", False) is True:
        return True
    status = _status_code(error)
    return status is not None and (status >= 500 or status == 429)


def call_with_retries(fn, *args, attempts=API_ATTEMPTS, sleep=time.sleep, **kwargs):
    """Call fn, retrying transient GitHub errors with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            if attempt == attempts or not is_retryable(error):
                raise
            delay = 2 ** attempt
            print(
                "WARN: GitHub API call failed ({}); retry {}/{} in {}s".format(
                    error, attempt, attempts - 1, delay
                )
            )
            sleep(delay)


def list_all_jobs_for_workflow_run(api, run_id, decode, per_page=JOBS_PER_PAGE, sleep=time.sleep):
    """Every job of a workflow run, following pagination.

    `decode` turns a ghapi response into a JSON string (custom_parser.do_fastcore_decode).
    Returns {"total_count": int, "jobs": [job, ...]}, the shape of one API page.
    Each page is retried on transient GitHub errors; if a page keeps failing, start over
    with half the page size (page numbers depend on the size, so earlier pages are refetched).
    """
    while True:
        try:
            return _list_job_pages(api, run_id, decode, per_page, sleep)
        except Exception as error:
            if not is_retryable(error) or per_page <= MIN_JOBS_PER_PAGE:
                raise
            smaller = max(MIN_JOBS_PER_PAGE, per_page // 2)
            print(
                "WARN: listing jobs with per_page={} kept failing ({}); retrying with per_page={}".format(
                    per_page, error, smaller
                )
            )
            per_page = smaller


def _list_job_pages(api, run_id, decode, per_page, sleep):
    jobs = []
    total_count = 0
    for page in range(1, MAX_JOB_PAGES + 1):
        response = json.loads(
            decode(
                call_with_retries(
                    api.actions.list_jobs_for_workflow_run,
                    run_id,
                    page=page,
                    per_page=per_page,
                    sleep=sleep,
                )
            )
        )
        page_jobs = response.get("jobs", [])
        total_count = response.get("total_count", len(page_jobs))
        jobs.extend(page_jobs)
        if not page_jobs or len(jobs) >= total_count:
            break
    else:
        print(
            "WARNING: stopped after {} pages of jobs ({} of {} jobs fetched)".format(
                MAX_JOB_PAGES, len(jobs), total_count
            )
        )
    return {"total_count": total_count, "jobs": jobs}
