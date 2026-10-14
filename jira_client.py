import os
import re
import time
import hashlib
import logging
from typing import Optional, Dict, Any, List, Tuple
import httpx

logger = logging.getLogger("jira-plans-mcp.client")

# Regex for safe Jira project keys, issue keys, or numeric IDs
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def sanitize_jql_value(value: str) -> str:
    """Sanitizes a string before interpolating it into a JQL query."""
    cleaned = value.strip()
    if _SAFE_KEY_RE.match(cleaned):
        return cleaned
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


class JiraClientError(Exception):
    """Base exception for Jira client operations."""
    pass


class JiraAuthError(JiraClientError):
    """Raised when authentication credentials are missing or rejected."""
    pass


class JiraNotFoundError(JiraClientError):
    """Raised when a requested resource (plan, issue) is not found."""
    pass


class JiraClient:
    """Stateless Atlassian Cloud REST API client driven strictly by per-request 3LO OAuth tokens."""

    _CACHE_TTL_SECONDS = 900  # 15 minutes
    _CACHE_MAX_SIZE = 256

    def __init__(self, timeout: Optional[float] = None):
        self.timeout = timeout or float(os.environ.get("JIRA_TIMEOUT", "20.0"))
        self.default_max_depth = int(os.environ.get("JIRA_MAX_DEPTH", "4"))
        self.default_max_results = int(os.environ.get("JIRA_MAX_RESULTS", "50"))

        # Bounded cache mapping SHA-256(bearer_token) -> (cloud_id, expiry_epoch).
        # Raw OAuth bearer tokens are never stored in memory beyond the request lifecycle.
        self._cloud_id_cache: Dict[str, Tuple[str, float]] = {}

        self._http_client = httpx.Client(
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
        )

    @staticmethod
    def _hash_token(token: str) -> str:
        """Computes a one-way SHA-256 digest of a bearer token for safe cache indexing."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _get_cached_cloud_id(self, token: str) -> Optional[str]:
        token_hash = self._hash_token(token)
        entry = self._cloud_id_cache.get(token_hash)
        if not entry:
            return None
        if isinstance(entry, tuple):
            cloud_id, expires_at = entry
            if time.time() < expires_at:
                return cloud_id
            self._cloud_id_cache.pop(token_hash, None)
            return None
        return str(entry)

    def _set_cached_cloud_id(self, token: str, cloud_id: str) -> None:
        if len(self._cloud_id_cache) >= self._CACHE_MAX_SIZE:
            oldest_key = next(iter(self._cloud_id_cache))
            self._cloud_id_cache.pop(oldest_key, None)
        token_hash = self._hash_token(token)
        self._cloud_id_cache[token_hash] = (cloud_id, time.time() + self._CACHE_TTL_SECONDS)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        max_retries: int = 2,
        **kwargs
    ) -> httpx.Response:
        """Executes an HTTP request using the shared connection pool with retry on 429/503."""
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                if method.upper() == "GET":
                    res = self._http_client.get(url, headers=headers, **kwargs)
                elif method.upper() == "POST":
                    res = self._http_client.post(url, headers=headers, **kwargs)
                else:
                    res = self._http_client.request(method, url, headers=headers, **kwargs)

                if res.status_code in (429, 502, 503, 504) and attempt < max_retries:
                    retry_after = res.headers.get("Retry-After") if hasattr(res, "headers") and res.headers else None
                    wait_sec = float(retry_after) if (retry_after and str(retry_after).isdigit()) else (0.5 * (2 ** attempt))
                    wait_sec = min(wait_sec, 5.0)
                    logger.warning(f"HTTP {res.status_code} from {url}, retrying in {wait_sec}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(wait_sec)
                    continue

                return res
            except httpx.RequestError as e:
                last_exc = e
                if attempt < max_retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise JiraClientError(f"Network error communicating with Atlassian Cloud: {e}") from e

        raise JiraClientError(f"Request to {url} failed after {max_retries} retries: {last_exc}")

    def _get_auth_headers_and_base_url(self, bearer_token: Optional[str] = None) -> Tuple[Dict[str, str], str]:
        """Resolves authentication headers and dynamic API base URL from the caller's 3LO OAuth token."""
        if not bearer_token or not bearer_token.strip():
            raise JiraAuthError(
                "No OAuth 2.0 Bearer token provided in request. Please authenticate via OAuth in Gemini Enterprise."
            )

        token = bearer_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        if not token:
            raise JiraAuthError(
                "Empty OAuth 2.0 Bearer token provided in request. Please authenticate via OAuth in Gemini Enterprise."
            )

        cloud_id = self._resolve_cloud_id(token)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        base = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        return headers, base

    def _resolve_cloud_id(self, token: str) -> str:
        """Dynamically discovers the accessible Jira Cloud ID from Atlassian for the current OAuth Bearer token."""
        cached = self._get_cached_cloud_id(token)
        if cached:
            return cached

        res = self._request_with_retry(
            "GET",
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        if res.status_code != 200:
            raise JiraAuthError(f"Failed to fetch accessible Atlassian resources: HTTP {res.status_code} - {res.text}")

        resources = res.json()
        if not resources or not isinstance(resources, list):
            raise JiraAuthError("No accessible Jira Cloud sites found for this authenticated user account.")

        chosen = resources[0]
        cloud_id = chosen["id"]
        self._set_cached_cloud_id(token, cloud_id)
        logger.info(f"Connected to Atlassian Cloud site '{chosen.get('name')}' (cloud_id: {cloud_id})")
        return cloud_id

    def list_plans(self, bearer_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists active Jira Plans (Advanced Roadmaps) and accessible project containers from live Jira Cloud."""
        headers, base = self._get_auth_headers_and_base_url(bearer_token)
        plans = []

        # 1. Query official Jira Cloud Plans REST API (/rest/api/3/plans/plan)
        for endpoint in ["/rest/api/3/plans/plan", "/rest/plans/1.0/plan"]:
            try:
                url = f"{base}{endpoint}"
                res = self._request_with_retry("GET", url, headers=headers)
                logger.info(f"Querying Jira Plans via {endpoint}: HTTP {res.status_code}")
                if res.status_code == 200:
                    data = res.json()
                    raw_plans = data.get("values", data) if isinstance(data, dict) else data
                    if isinstance(raw_plans, list) and len(raw_plans) > 0:
                        for p in raw_plans:
                            lead_val = p.get("lead", {}) or p.get("leadAccountId", "Unassigned")
                            lead_name = lead_val.get("displayName", "Unassigned") if isinstance(lead_val, dict) else str(lead_val)
                            plans.append({
                                "plan_id": str(p.get("id")),
                                "name": p.get("name", f"Plan {p.get('id')}"),
                                "lead": lead_name,
                                "target_start": p.get("targetStartDate"),
                                "target_end": p.get("targetEndDate"),
                                "status": p.get("status", "ACTIVE"),
                                "is_native_plan": True
                            })
                        logger.info(f"Found {len(plans)} native Jira Plans via {endpoint}.")
                        break
                else:
                    logger.warning(f"Jira Plans endpoint {endpoint} returned HTTP {res.status_code}: {res.text[:200]}")
            except Exception as e:
                logger.warning(f"Error querying {endpoint}: {e}")

        if plans:
            return plans

        # 2. If no formal Plans exist, discover accessible Projects as roadmap containers
        try:
            res = self._request_with_retry("GET", f"{base}/rest/api/3/project", headers=headers)
            if res.status_code == 200:
                projects = res.json()
                for proj in projects:
                    plans.append({
                        "plan_id": proj.get("key"),
                        "name": proj.get("name", proj.get("key")),
                        "lead": proj.get("lead", {}).get("displayName", "Project Lead") if isinstance(proj.get("lead"), dict) else "Project Lead",
                        "target_start": None,
                        "target_end": None,
                        "status": "ACTIVE",
                        "project_key": proj.get("key"),
                        "project_type": proj.get("projectTypeKey", "software")
                    })
        except Exception as e:
            logger.error(f"Error querying live Jira projects: {e}")

        return plans

    def get_plan(self, plan_id: str, bearer_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetches details for a specific native Jira Plan by ID or name, including its issue sources."""
        headers, base = self._get_auth_headers_and_base_url(bearer_token)
        raw_target = plan_id.strip()
        numeric_id = raw_target if raw_target.isdigit() else None

        if not numeric_id:
            try:
                plans = self.list_plans(bearer_token=bearer_token)
                for p in plans:
                    if p.get("name", "").lower() == raw_target.lower() or p.get("plan_id") == raw_target:
                        numeric_id = p.get("plan_id")
                        break
            except Exception as e:
                logger.warning(f"Error resolving plan name '{raw_target}': {e}")

        if numeric_id and str(numeric_id).isdigit():
            for endpoint in [f"/rest/api/3/plans/plan/{numeric_id}", f"/rest/plans/1.0/plan/{numeric_id}"]:
                try:
                    res = self._request_with_retry("GET", f"{base}{endpoint}", headers=headers)
                    if res.status_code == 200:
                        return res.json()
                except Exception as e:
                    logger.warning(f"Failed fetching plan details from {endpoint}: {e}")
        return None

    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: Optional[int] = None,
        bearer_token: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Executes a live JQL search query on Jira Cloud using the modern /rest/api/3/search/jql endpoint."""
        headers, base = self._get_auth_headers_and_base_url(bearer_token)
        default_fields = [
            "summary", "status", "issuetype", "parent", "assignee",
            "priority", "subtasks", "issuelinks", "duedate"
        ]
        target_fields = fields or default_fields
        limit = max_results if max_results is not None else self.default_max_results

        payload = {
            "jql": jql,
            "fields": target_fields,
            "maxResults": limit
        }

        # 1. Modern Atlassian Jira Cloud Search API: POST /rest/api/3/search/jql
        res = self._request_with_retry("POST", f"{base}/rest/api/3/search/jql", headers=headers, json=payload)

        # 2. Fallback: GET /rest/api/3/search/jql
        if res.status_code != 200:
            logger.warning(f"POST /rest/api/3/search/jql returned {res.status_code}: {res.text}. Trying GET /rest/api/3/search/jql...")
            params = {"jql": jql, "fields": ",".join(target_fields), "maxResults": str(limit)}
            res = self._request_with_retry("GET", f"{base}/rest/api/3/search/jql", headers=headers, params=params)

        # 3. Fallback: legacy POST /rest/api/3/search (if not 410 Gone)
        if res.status_code != 200 and res.status_code != 410:
            logger.warning(f"GET /rest/api/3/search/jql returned {res.status_code}. Trying legacy /rest/api/3/search...")
            res = self._request_with_retry("POST", f"{base}/rest/api/3/search", headers=headers, json=payload)

        if res.status_code != 200:
            raise JiraClientError(f"Jira search failed (HTTP {res.status_code}): {res.text}")

        data = res.json()
        return data.get("issues", [])

    def get_issue(self, issue_key: str, bearer_token: Optional[str] = None) -> Dict[str, Any]:
        """Fetches complete issue details from Jira Cloud."""
        headers, base = self._get_auth_headers_and_base_url(bearer_token)
        safe_key = sanitize_jql_value(issue_key)
        fields = "summary,status,issuetype,parent,assignee,priority,subtasks,issuelinks,duedate,description"

        res = self._request_with_retry(
            "GET",
            f"{base}/rest/api/3/issue/{safe_key}",
            headers=headers,
            params={"fields": fields}
        )
        if res.status_code == 404:
            raise JiraNotFoundError(f"Issue '{issue_key}' was not found in Jira Cloud.")
        if res.status_code != 200:
            raise JiraClientError(f"Failed to fetch issue '{issue_key}' (HTTP {res.status_code}): {res.text}")

        return res.json()

    def get_child_issues(self, parent_key: str, bearer_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """Queries all direct children of an issue (Initiatives -> Epics -> Stories/Tasks) using JQL."""
        safe_parent = sanitize_jql_value(parent_key)
        jql = f'parent = "{safe_parent}" ORDER BY created ASC'
        try:
            return self.search_issues(jql=jql, max_results=100, bearer_token=bearer_token)
        except Exception as e:
            logger.warning(f"Failed querying child issues for parent {parent_key}: {e}")
            return []

    def recursively_build_subtree(
        self,
        issue: Dict[str, Any],
        bearer_token: Optional[str] = None,
        ancestor_path: Optional[List[Dict[str, str]]] = None,
        visited_keys: Optional[set] = None,
        depth: int = 0,
        max_depth: Optional[int] = None
    ) -> Dict[str, Any]:
        """Recursively traverses a live Jira issue down to terminal leaves."""
        effective_max_depth = max_depth if max_depth is not None else self.default_max_depth
        visited = visited_keys if visited_keys is not None else set()
        ancestors = ancestor_path or []

        issue_key = issue.get("key", "UNKNOWN")
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        status_obj = fields.get("status", {})
        status_name = status_obj.get("name", "To Do") if isinstance(status_obj, dict) else str(status_obj)
        assignee_obj = fields.get("assignee")
        assignee = assignee_obj.get("displayName", "Unassigned") if isinstance(assignee_obj, dict) else "Unassigned"
        issue_type_obj = fields.get("issuetype", {})
        issue_type = issue_type_obj.get("name", "Task") if isinstance(issue_type_obj, dict) else "Task"

        current_lineage = ancestors + [{"key": issue_key, "summary": summary, "type": issue_type}]
        visited.add(issue_key)

        # Check for blockers
        status_upper = status_name.upper()
        is_blocked = "BLOCKED" in status_upper or "BLOCK" in status_upper
        blocker_reason = None

        issuelinks = fields.get("issuelinks", [])
        for link in issuelinks:
            inward = link.get("inwardIssue")
            type_info = link.get("type", {})
            if inward and "block" in type_info.get("inward", "").lower():
                is_blocked = True
                blocker_reason = f"Blocked by {inward.get('key')}: {inward.get('fields', {}).get('summary', '')}"
                break

        # Check for terminal leaves: subtasks or children
        raw_subtasks = fields.get("subtasks", [])
        child_issues = []

        if depth < effective_max_depth:
            if raw_subtasks:
                for sub in raw_subtasks:
                    sub_key = sub.get("key")
                    if sub_key and sub_key not in visited:
                        if "fields" in sub and "summary" in sub.get("fields", {}):
                            child_issues.append(sub)
                        else:
                            try:
                                sub_full = self.get_issue(sub_key, bearer_token=bearer_token)
                                child_issues.append(sub_full)
                            except Exception:
                                child_issues.append(sub)
            else:
                children = self.get_child_issues(issue_key, bearer_token=bearer_token)
                for child in children:
                    if child.get("key") not in visited:
                        child_issues.append(child)

        # If no children found, this is a terminal leaf node
        if not child_issues:
            is_done = status_upper in ("DONE", "RESOLVED", "CLOSED")
            is_in_prog = status_upper in ("IN PROGRESS", "IN DEVELOPMENT", "ACTIVE")
            is_todo = not is_done and not is_in_prog and not is_blocked

            leaf_item = {
                "leaf_key": issue_key,
                "summary": summary,
                "type": issue_type,
                "status": status_name,
                "assignee": assignee,
                "is_blocked": is_blocked,
                "blocker_reason": blocker_reason,
                "lineage": current_lineage
            }

            return {
                "node_key": issue_key,
                "summary": summary,
                "type": issue_type,
                "status": status_name,
                "assignee": assignee,
                "is_leaf": True,
                "is_blocked": is_blocked,
                "blocker_reason": blocker_reason,
                "metrics": {
                    "total_leaves": 1,
                    "done_leaves": 1 if is_done else 0,
                    "in_progress_leaves": 1 if is_in_prog else 0,
                    "blocked_leaves": 1 if is_blocked else 0,
                    "todo_leaves": 1 if is_todo else 0,
                    "progress_pct": 100.0 if is_done else 0.0
                },
                "leaves": [leaf_item],
                "blockers": [leaf_item] if is_blocked else [],
                "children": []
            }

        # Non-terminal node: recurse into children
        resolved_children = []
        accumulated_leaves = []
        accumulated_blockers = []

        for child in child_issues:
            resolved_child = self.recursively_build_subtree(
                issue=child,
                bearer_token=bearer_token,
                ancestor_path=current_lineage,
                visited_keys=visited,
                depth=depth + 1,
                max_depth=effective_max_depth
            )
            resolved_children.append(resolved_child)
            accumulated_leaves.extend(resolved_child["leaves"])
            accumulated_blockers.extend(resolved_child["blockers"])

        total_l = len(accumulated_leaves)
        done_l = sum(1 for l in accumulated_leaves if l["status"].upper() in ("DONE", "RESOLVED", "CLOSED"))
        in_prog_l = sum(1 for l in accumulated_leaves if l["status"].upper() in ("IN PROGRESS", "IN DEVELOPMENT", "ACTIVE"))
        blocked_l = sum(1 for l in accumulated_leaves if l["is_blocked"])
        todo_l = total_l - done_l - in_prog_l - blocked_l
        progress_pct = round((done_l / total_l * 100.0), 1) if total_l > 0 else 0.0

        return {
            "node_key": issue_key,
            "summary": summary,
            "type": issue_type,
            "status": status_name,
            "assignee": assignee,
            "is_leaf": False,
            "metrics": {
                "total_leaves": total_l,
                "done_leaves": done_l,
                "in_progress_leaves": in_prog_l,
                "blocked_leaves": blocked_l,
                "todo_leaves": todo_l,
                "progress_pct": progress_pct
            },
            "blockers_count": len(accumulated_blockers),
            "leaves": accumulated_leaves,
            "blockers": accumulated_blockers,
            "children": resolved_children
        }
