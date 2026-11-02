"""Creates real Asana tasks via the Asana API (app.asana.com/api/1.0/tasks).

Credentials (ASANA_ACCESS_TOKEN, ASANA_PROJECT_ID) are read from .env fresh on
every call (override=True) rather than cached at import time, so a value added
to .env after this process started still gets picked up without a restart --
same pattern as royal_mail_tracking.py's _config().
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

ASANA_TASKS_URL = "https://app.asana.com/api/1.0/tasks"

# Classification projects (see asana_task_formatter.classify_claim_projects)
# are multi-homed onto the task alongside the main project, by adding the
# task to each of these projects rather than setting a custom field -- the
# team's board uses one project per ODR/Loss/Action Stage value, not a
# dropdown field on a single project.
_CLASSIFICATION_PROJECT_ENV_VARS = {
    "odr_unknown": "ASANA_PROJECT_ODR_UNKNOWN",
    "loss_yes": "ASANA_PROJECT_LOSS_YES",
    "loss_no": "ASANA_PROJECT_LOSS_NO",
    "action_stage_to_be_made": "ASANA_PROJECT_ACTION_STAGE_TO_BE_MADE",
    "customer_payment_contact_required": "ASANA_PROJECT_CUSTOMER_PAYMENT_CONTACT_REQUIRED",
    # 2nd-appeal flow (a "Claim decision" email's ODR Affect flag + the
    # order's DC status) -- loss_yes/loss_no above are reused as-is (same
    # Dispatched/not-Dispatched rule), these two and the action stage below
    # are specific to this flow.
    "odr_yes": "ASANA_PROJECT_ODR_YES",
    "odr_no": "ASANA_PROJECT_ODR_NO",
    "action_stage_amz_claim_in_progress": "ASANA_PROJECT_ACTION_STAGE_AMZ_CLAIM_IN_PROGRESS",
}


_cached_workspace_gid: str | None = None


def _config() -> dict[str, str]:
    load_dotenv(override=True)
    token = os.getenv("ASANA_ACCESS_TOKEN", "").strip()
    project_id = os.getenv("ASANA_PROJECT_ID", "").strip()
    missing = [name for name, value in [("ASANA_ACCESS_TOKEN", token), ("ASANA_PROJECT_ID", project_id)] if not value]
    if missing:
        raise RuntimeError(f"Missing Asana credential(s) in .env: {', '.join(missing)}")
    return {"token": token, "project_id": project_id}


def resolve_project_gids(classification_keys: list[str]) -> list[str]:
    """Maps classify_claim_projects' semantic keys to the real Asana project
    GIDs configured in .env. Raises if a key is used but its env var isn't
    set, so a misconfigured mapping fails loudly instead of silently
    skipping a project the task should have been added to."""
    load_dotenv(override=True)
    gids: list[str] = []
    for key in classification_keys:
        env_var = _CLASSIFICATION_PROJECT_ENV_VARS[key]
        gid = os.getenv(env_var, "").strip()
        if not gid:
            raise RuntimeError(f"Missing Asana credential in .env: {env_var} (needed for classification '{key}')")
        gids.append(gid)
    return gids


def _workspace_gid() -> str:
    """Resolves the main project's workspace gid once per process (cached --
    it never changes) rather than requiring a separate ASANA_WORKSPACE_ID
    env var, confirmed live: GET /projects/{id}?opt_fields=workspace.gid."""
    global _cached_workspace_gid
    if _cached_workspace_gid is None:
        config = _config()
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"https://app.asana.com/api/1.0/projects/{config['project_id']}",
                headers={"Authorization": f"Bearer {config['token']}"},
                params={"opt_fields": "workspace.gid"},
            )
        response.raise_for_status()
        _cached_workspace_gid = response.json()["data"]["workspace"]["gid"]
    return _cached_workspace_gid


def list_project_tasks(project_gid: str) -> list[dict]:
    """Every task in one project ({'name', 'permalink_url', 'completed'}
    per task), paginated (100/page) -- used to bulk-build an order-number
    -> task map covering ALL classification projects at once (see
    pipeline_ui._ensure_asana_bulk_cache), instead of one live typeahead
    search per untasked order. Confirmed as the real cause of the orders
    list still feeling like it's "running" well after a fetch finishes:
    with ~45 untasked 2nd-appeal cards in one real batch, the first render
    each session fired that many sequential find_task_by_order_number
    calls, each its own network round trip."""
    config = _config()
    tasks: list[dict] = []
    offset: str | None = None
    with httpx.Client(timeout=30.0) as client:
        while True:
            params = {"opt_fields": "name,permalink_url,completed,due_on,parent", "limit": 100}
            if offset:
                params["offset"] = offset
            response = client.get(
                f"https://app.asana.com/api/1.0/projects/{project_gid}/tasks",
                headers={"Authorization": f"Bearer {config['token']}"},
                params=params,
            )
            response.raise_for_status()
            body = response.json()
            tasks.extend(body.get("data") or [])
            offset = (body.get("next_page") or {}).get("offset")
            if not offset:
                break
    return tasks


def find_task_by_name_in_project(project_gid: str, name: str) -> dict | None:
    """Exact-name match among this project's own, currently NOT-completed
    tasks -- used for daily recurring checklist tasks (e.g. "A to Z
    Schedular 1st Half"/"2nd Half" in the team's "Automation Tasks"
    project), which Asana regenerates as a brand-new task gid every day
    (confirmed live: created_at/due_on are today's date on the gid
    checked, not some fixed task reused daily) -- so a scheduler ticking
    one of these off can never hardcode the gid, only the project + its
    exact, stable name. Returns None if no incomplete task with this
    exact name exists right now (already ticked, or the checklist
    doesn't have this item today)."""
    tasks = list_project_tasks(project_gid)
    for task in tasks:
        if not task.get("completed") and (task.get("name") or "").strip() == name:
            return task
    return None


# Other teams run their own, unrelated Asana workflows that also happen to
# put the bare order number in a task's name -- confirmed live on order
# 206-7248103-1989104: find_task_by_order_number's substring match picked
# up "Amazon Negative Feedback - 206-7248103-1989104" (filed under this
# project, nothing to do with a claim/appeal) instead of ever creating this
# project's own real task, since the search found *a* match and stopped.
# Any task living in one of these projects is never a real claim/appeal
# task, no matter how closely its name matches.
EXCLUDED_PROJECT_NAMES = {"Follow Up (feedbacks)"}


def find_task_by_order_number(order_number: str) -> dict | None:
    """Asks Asana itself whether a task already exists for this order,
    instead of relying on local persistence (session_state/orders_store)
    that can go stale -- e.g. a task created before a persistence fix
    existed, or in a since-restarted session. Uses the workspace typeahead
    endpoint (GET /workspaces/{gid}/typeahead?resource_type=task&query=...),
    confirmed live to do a genuine substring match against task names (not
    prefix-only -- a bare order-number fragment matched a real task whose
    name only contained it mid-string), and to respect this token's actual
    task access (a task it can't see is simply never returned).

    Every task this project creates has the order number literally in its
    name (build_task_name), so this is a reliable "does a task already
    exist for this order" check across BOTH the 1st-representation and
    2nd-appeal flows, without needing to know which project/flow it was
    filed under -- EXCEPT for a task that happens to belong to one of
    EXCLUDED_PROJECT_NAMES, a different team's workflow that also stamps
    the order number into its task names (see that set's own comment).
    Those are skipped in favour of the next candidate, never returned.

    Returns the first {'task_gid', 'task_url'} match, or None if Asana has
    no (non-excluded) task with this order number in its name."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"https://app.asana.com/api/1.0/workspaces/{_workspace_gid()}/typeahead",
            headers={"Authorization": f"Bearer {config['token']}"},
            params={
                "resource_type": "task", "query": order_number,
                "opt_fields": "name,permalink_url,memberships.project.name", "count": 10,
            },
        )
    response.raise_for_status()
    for task in response.json().get("data") or []:
        if order_number not in (task.get("name") or ""):
            continue
        project_names = {m["project"]["name"] for m in task.get("memberships") or [] if m.get("project")}
        if project_names & EXCLUDED_PROJECT_NAMES:
            continue
        return {"task_gid": task["gid"], "task_url": task.get("permalink_url")}
    return None


def create_task(name: str, notes: str, extra_project_gids: list[str] | None = None) -> dict:
    """Returns {'created': bool, 'task_gid': str | None, 'task_url': str | None, 'error': str | None}.

    Only ever homed into the classification projects passed in
    extra_project_gids (ODR/Loss/Action Stage -- see
    asana_task_formatter.classify_claim_projects/classify_decision_projects)
    -- ASANA_PROJECT_ID is kept in .env only to resolve the workspace gid
    (see _workspace_gid), not as a project every task gets added to. It
    used to be added here unconditionally too, which turned out to point
    at a project literally named "Routine Tasks" -- every claim task was
    silently filed there as well, never asked for."""
    config = _config()
    projects = list(extra_project_gids or [])
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            ASANA_TASKS_URL,
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"name": name, "notes": notes, "projects": projects}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"created": False, "task_gid": None, "task_url": None, "error": message}
    data = body.get("data", {})
    return {"created": True, "task_gid": data.get("gid"), "task_url": data.get("permalink_url"), "error": None}


def find_subtask_by_name(parent_gid: str, name: str) -> dict | None:
    """Looks for an existing subtask of parent_gid with this exact name
    (GET /tasks/{parent_gid}/subtasks) -- used so the "Amazon A-Z - <order>"
    appeal-log subtask (see pipeline_ui._after_real_submit) is updated in
    place on a 2nd/3rd+ appeal rather than duplicated. A claim task only
    ever has a handful of subtasks, so no pagination is needed.

    Returns {'task_gid', 'task_url', 'notes'} or None if no subtask has
    this exact name."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{ASANA_TASKS_URL}/{parent_gid}/subtasks",
            headers={"Authorization": f"Bearer {config['token']}"},
            params={"opt_fields": "name,notes,permalink_url", "limit": 100},
        )
    response.raise_for_status()
    for task in response.json().get("data") or []:
        if task.get("name") == name:
            return {"task_gid": task["gid"], "task_url": task.get("permalink_url"), "notes": task.get("notes") or ""}
    return None


def find_amazon_az_subtask(parent_gid: str, order_number: str) -> dict | None:
    """Like find_subtask_by_name, but tolerant of real-world spacing drift
    around the dash -- confirmed live that existing "Amazon A-Z" subtasks
    use several different exact spacings ("Amazon A-Z- <order>", "Amazon
    A-Z  - <order> " with a trailing space, etc., never consistently the
    "Amazon A-Z - <order>" build_amazon_az_subtask_name produces), which
    silently made find_subtask_by_name's exact match miss real subtasks
    that do exist (confirmed on 8 real orders at once). Matches any
    subtask whose name, with whitespace collapsed, starts with "amazon
    a-z" case-insensitively and contains the order number."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{ASANA_TASKS_URL}/{parent_gid}/subtasks",
            headers={"Authorization": f"Bearer {config['token']}"},
            params={"opt_fields": "name,notes,permalink_url", "limit": 100},
        )
    response.raise_for_status()
    for task in response.json().get("data") or []:
        name = task.get("name") or ""
        normalized = re.sub(r"\s+", " ", name).strip().lower()
        if normalized.startswith("amazon a-z") and order_number in name:
            return {"task_gid": task["gid"], "task_url": task.get("permalink_url"), "notes": task.get("notes") or ""}
    return None


def create_subtask(parent_gid: str, name: str, notes: str) -> dict:
    """Creates a real subtask under parent_gid (POST /tasks/{parent_gid}/subtasks),
    same mechanism Asana's own "Add subtask" button uses -- confirmed real
    pattern against a real "Amazon A-Z - <order>" subtask (gid
    1218854488428281) nested under its order's main claim task.

    Returns {'created': bool, 'task_gid': str | None, 'task_url': str | None, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{ASANA_TASKS_URL}/{parent_gid}/subtasks",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"name": name, "notes": notes}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"created": False, "task_gid": None, "task_url": None, "error": message}
    data = body.get("data", {})
    return {"created": True, "task_gid": data.get("gid"), "task_url": data.get("permalink_url"), "error": None}


def update_task_notes(task_gid: str, notes: str) -> dict:
    """Overwrites an existing task's notes (PUT /tasks/{task_gid}) -- used to
    append a new "<Nth> appeal done on <date>" line to the "Amazon A-Z -
    <order>" subtask's running log on each real appeal re-submit.

    Returns {'updated': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.put(
            f"{ASANA_TASKS_URL}/{task_gid}",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"notes": notes}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"updated": False, "error": message}
    return {"updated": True, "error": None}


def set_due_date(task_gid: str, due_on: str) -> dict:
    """Sets (or clears, due_on="") a task's due date (PUT /tasks/{task_gid},
    due_on="YYYY-MM-DD") -- used by reappeal_scheduler.py to push the main
    claim task's due date out one week after every re-appeal round, so the
    task itself visibly shows when it's next due back on someone's radar
    instead of that living only in the subtask's notes.

    Returns {'updated': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.put(
            f"{ASANA_TASKS_URL}/{task_gid}",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"due_on": due_on}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"updated": False, "error": message}
    return {"updated": True, "error": None}


def add_comment(task_gid: str, text: str) -> dict:
    """Posts a comment (Asana "story") on an existing task -- confirmed real
    pattern from a real 2nd-appeal task: the claim decision email's full
    body is posted as a comment, separate from the task's notes (which only
    ever hold the address/DC-status/product block, never the email text).

    Returns {'added': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}/stories",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"text": text}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"added": False, "error": message}
    return {"added": True, "error": None}


def add_attachment(task_gid: str, file_path: str) -> dict:
    """Uploads a local file (e.g. the claim-page "SS (A-Z)" screenshot) as a
    real attachment on an existing task -- POST /tasks/{task_gid}/attachments,
    multipart/form-data, same endpoint Asana's own upload button uses.
    Distinct from add_comment: this is a visible attachment on the task, not
    a comment linking/describing one.

    Returns {'added': bool, 'error': str | None}."""
    config = _config()
    path = Path(file_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as f, httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ASANA_TASKS_URL}/{task_gid}/attachments",
            headers={"Authorization": f"Bearer {config['token']}"},
            files={"file": (path.name, f, content_type)},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"added": False, "error": message}
    return {"added": True, "error": None}


def list_comments(task_gid: str) -> list[dict]:
    """Every real comment ("story" of type "comment") on a task, oldest
    first -- used by reappeal_scheduler.py to find the full appeal
    text/Drive-link comment already posted on the "Amazon A-Z - <order>"
    subtask for the LATEST round, so a later round's Help Hub case can
    reuse that exact text as "what's changed since the last appeal"
    instead of it being retyped or guessed at.

    Returns [{'text': str, 'created_at': str}, ...]."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{ASANA_TASKS_URL}/{task_gid}/stories",
            headers={"Authorization": f"Bearer {config['token']}"},
            params={"opt_fields": "text,type,created_at", "limit": 100},
        )
    response.raise_for_status()
    return [
        {"text": story.get("text") or "", "created_at": story.get("created_at")}
        for story in response.json().get("data") or []
        if story.get("type") == "comment"
    ]


def add_mention_comment(task_gid: str, user_gid: str) -> dict:
    """Posts a comment that @mentions a specific user -- confirmed real
    pattern on a real 2nd-appeal task (after the email-body comment, two
    further comments each just @mention one person). Asana renders the
    mention from data-asana-gid alone; html_text must be wrapped in <body>.

    Returns {'added': bool, 'error': str | None}."""
    config = _config()
    html_text = f'<body><a data-asana-gid="{user_gid}"/></body>'
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}/stories",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"html_text": html_text}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"added": False, "error": message}
    return {"added": True, "error": None}


def complete_task(task_gid: str) -> dict:
    """Marks an existing task as completed (PUT /tasks/{task_gid}, {"completed": true}).

    Returns {'completed': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.put(
            f"{ASANA_TASKS_URL}/{task_gid}",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"completed": True}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"completed": False, "error": message}
    return {"completed": True, "error": None}


def add_project(task_gid: str, project_gid: str) -> dict:
    """Adds an existing task to another project -- used to move a task's
    Action Stage from "To Be Made" to "AMZ Claim in Progress" once a real
    Amazon submit succeeds (see pipeline_ui._after_real_submit). Idempotent
    in practice: Asana errors if the task is already in that project, which
    is harmless here and just logged by the caller, never raised.

    Returns {'added': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}/addProject",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"project": project_gid}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"added": False, "error": message}
    return {"added": True, "error": None}


def remove_project(task_gid: str, project_gid: str) -> dict:
    """Removes an existing task from a project -- the other half of the
    Action Stage swap in add_project above. A no-op (Asana returns success)
    if the task was never in that project, so it's safe to call
    unconditionally rather than first checking membership.

    Returns {'removed': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}/removeProject",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"project": project_gid}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"removed": False, "error": message}
    return {"removed": True, "error": None}


def set_parent(task_gid: str, parent_gid: str) -> dict:
    """Re-parents an existing task/subtask (POST /tasks/{task_gid}/setParent)
    -- needed to fix a subtask created under the wrong parent (confirmed
    live: find_task_by_order_number's old wrong-project-match bug, see
    EXCLUDED_PROJECT_NAMES, meant an Amazon A-Z subtask got created under
    an unrelated "Amazon Negative Feedback" task instead of the real claim
    task for order 206-7248103-1989104).

    Returns {'reparented': bool, 'error': str | None}."""
    config = _config()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{ASANA_TASKS_URL}/{task_gid}/setParent",
            headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
            json={"data": {"parent": parent_gid}},
        )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        message = (body.get("errors") or [{}])[0].get("message") or f"HTTP {response.status_code}"
        return {"reparented": False, "error": message}
    return {"reparented": True, "error": None}


if __name__ == "__main__":
    import sys

    print(create_task(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
