import json

from ghapi.all import GhApi

# GitHub returns at most 100 jobs per page.
JOBS_PER_PAGE = 100
# Safety stop for a misbehaving API: 100 pages is 10,000 jobs, far above a real run.
MAX_JOB_PAGES = 100


def create_github_api_client(owner, repo, token):
    return GhApi(owner=owner, repo=repo, token=str(token), sync=True)


def list_all_jobs_for_workflow_run(api, run_id, decode, per_page=JOBS_PER_PAGE):
    """Every job of a workflow run, following pagination.

    `decode` turns a ghapi response into a JSON string (custom_parser.do_fastcore_decode).
    Returns {"total_count": int, "jobs": [job, ...]}, the shape of one API page.
    """
    jobs = []
    total_count = 0
    for page in range(1, MAX_JOB_PAGES + 1):
        response = json.loads(
            decode(api.actions.list_jobs_for_workflow_run(run_id, page=page, per_page=per_page))
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
