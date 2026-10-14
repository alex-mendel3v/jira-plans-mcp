import os
import json
import logging
from contextvars import ContextVar
from typing import Optional
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager, StreamableHTTPASGIApp
import uvicorn

from jira_client import JiraClient, JiraAuthError, JiraNotFoundError, sanitize_jql_value

log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level_name, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("jira-plans-mcp")

# Context variable to hold the Bearer token propagated from Gemini Enterprise
auth_token_cv: ContextVar[Optional[str]] = ContextVar("auth_token_cv", default=None)

# Initialize Live Jira Client
jira_client = JiraClient()

# ============================================================================
# Native FastMCP Server Definition (Live Atlassian Data Server)
# ============================================================================
mcp_server = MCPServer(
    name="jira-plans-mcp",
    description="Live Jira Plans & Multi-Tier Hierarchy MCP Data Server for Gemini Enterprise"
)


def get_current_bearer_token() -> Optional[str]:
    """Retrieves the active authorization header from the current request context."""
    return auth_token_cv.get()


@mcp_server.tool(
    name="traverse_hierarchy",
    description="Universal recursive hierarchy traversal engine for live Jira. Takes any arbitrary Jira issue keys, plan IDs, or project keys accessible to the authenticated user and recursively traverses down to all lowest leaf sub-tasks, computing real-time rollup metrics, tree structures, and critical path blockers in structured JSON."
)
def traverse_hierarchy(
    issue_keys: Optional[str] = "all",
    project_key: Optional[str] = None,
    include_full_tree: bool = True
) -> str:
    """Recursively traverses live Jira structures down to terminal leaf items."""
    token = get_current_bearer_token()
    keys_list = [k.strip().upper() for k in issue_keys.split(",") if k.strip()] if issue_keys and issue_keys.lower() != "all" else []
    proj_upper = project_key.strip().upper() if project_key else None

    root_issues = []

    try:
        if keys_list:
            for k in keys_list:
                try:
                    iss = jira_client.get_issue(k, bearer_token=token)
                    root_issues.append(iss)
                except JiraNotFoundError:
                    logger.warning(f"Key {k} not found in Jira.")
        elif proj_upper:
            # Check if proj_upper is a native Jira Plan name or ID
            plan_meta = jira_client.get_plan(proj_upper, bearer_token=token)
            if plan_meta:
                sources = plan_meta.get("issueSources", [])
                proj_sources = [str(s.get("value")) for s in sources if s.get("type", "").lower() in ("project", "projects")]
                if proj_sources:
                    projects = [sanitize_jql_value(p.strip().upper()) for p in proj_sources if p.strip()]
                else:
                    projects = [sanitize_jql_value(proj_upper)]
            else:
                # Handle comma-separated project keys
                projects = [sanitize_jql_value(p.strip().upper()) for p in proj_upper.split(",") if p.strip()]

            if len(projects) > 1:
                proj_clause = f'project in ({", ".join(f"{p}" if p.isdigit() else f"{chr(34)}{p}{chr(34)}" for p in projects)})'
            else:
                p0 = projects[0]
                proj_clause = f'project = {p0}' if p0.isdigit() else f'project = "{p0}"'

            # 1. Discover top-level root issues (parent is EMPTY)
            try:
                jql = f'{proj_clause} AND parent is EMPTY ORDER BY created DESC'
                root_issues = jira_client.search_issues(jql=jql, max_results=25, bearer_token=token)
            except Exception as e:
                logger.warning(f"Root query with 'parent is EMPTY' failed for {proj_clause}: {e}")
                root_issues = []

            if not root_issues:
                # 2. Broader fallback: any issues in project(s)
                try:
                    jql = f'{proj_clause} ORDER BY created DESC'
                    root_issues = jira_client.search_issues(jql=jql, max_results=15, bearer_token=token)
                except Exception as e:
                    logger.warning(f"Fallback project query failed: {e}")
        else:
            # Query recent top-level issues across all accessible projects
            try:
                jql = 'parent is EMPTY ORDER BY created DESC'
                root_issues = jira_client.search_issues(jql=jql, max_results=20, bearer_token=token)
            except Exception as e:
                logger.warning(f"Global root query failed: {e}")
                root_issues = []

            if not root_issues:
                try:
                    root_issues = jira_client.search_issues(jql='ORDER BY created DESC', max_results=10, bearer_token=token)
                except Exception as e:
                    logger.warning(f"Global fallback query failed: {e}")

        if not root_issues:
            return json.dumps({
                "status": "empty",
                "message": "No matching live Jira issues found for the specified keys or project.",
                "keys_searched": keys_list,
                "project_key": project_key,
                "traversed_structures": []
            }, indent=2)

        resolved_trees = []
        total_leaves = 0
        total_done = 0
        total_in_prog = 0
        total_blocked = 0
        total_todo = 0
        all_blockers = []

        for issue in root_issues:
            tree = jira_client.recursively_build_subtree(issue=issue, bearer_token=token)
            resolved_trees.append(tree)
            m = tree["metrics"]
            total_leaves += m["total_leaves"]
            total_done += m["done_leaves"]
            total_in_prog += m["in_progress_leaves"]
            total_blocked += m["blocked_leaves"]
            total_todo += m["todo_leaves"]
            all_blockers.extend(tree["blockers"])

        overall_progress_pct = round((total_done / total_leaves * 100.0), 1) if total_leaves > 0 else 0.0

        payload = {
            "summary_metrics": {
                "root_nodes_evaluated": len(resolved_trees),
                "total_leaf_subtasks": total_leaves,
                "completed_leaves": total_done,
                "in_progress_leaves": total_in_prog,
                "blocked_leaves": total_blocked,
                "todo_leaves": total_todo,
                "overall_progress_pct": overall_progress_pct,
                "critical_blockers_count": len(all_blockers)
            },
            "critical_blockers": all_blockers,
            "traversed_structures": [
                {
                    "root_key": t["node_key"],
                    "summary": t["summary"],
                    "type": t["type"],
                    "status": t["status"],
                    "assignee": t["assignee"],
                    "metrics": t["metrics"],
                    "children": t["children"] if include_full_tree else []
                }
                for t in resolved_trees
            ]
        }
        return json.dumps(payload, indent=2)

    except JiraAuthError as e:
        return json.dumps({"error": "Authentication Failed", "details": str(e)}, indent=2)
    except Exception as e:
        logger.error(f"Error in traverse_hierarchy: {e}", exc_info=True)
        return json.dumps({"error": "Failed to traverse live Jira hierarchy", "details": str(e)}, indent=2)


@mcp_server.tool(
    name="list_plans",
    description="List all available live Jira Plans (Advanced Roadmaps) and active project roadmap containers with their IDs, owners, timeline dates, and status in structured JSON."
)
def list_plans() -> str:
    """Lists all active Jira Plans from live Jira Cloud."""
    token = get_current_bearer_token()
    try:
        plans = jira_client.list_plans(bearer_token=token)
        return json.dumps({
            "plans": plans,
            "total_plans": len(plans),
            "source": "live_atlassian_cloud"
        }, indent=2)
    except JiraAuthError as e:
        return json.dumps({"error": "Authentication Failed", "details": str(e)}, indent=2)
    except Exception as e:
        logger.error(f"Error listing plans: {e}")
        return json.dumps({"error": "Failed to list live Jira plans", "details": str(e)}, indent=2)


@mcp_server.tool(
    name="get_plan_hierarchy",
    description="Get the complete multi-tier hierarchy breakdown of a live Jira Plan or Project (Plan -> Initiatives -> Epics -> Stories -> Leaf Subtasks) with status rollups in structured JSON."
)
def get_plan_hierarchy(plan_id: str) -> str:
    """Traverses down the full nested hierarchy tree of a live Jira Plan or Project."""
    token = get_current_bearer_token()
    try:
        # Determine if plan_id is an issue key or project key
        target_id = plan_id.strip()

        # Check if plan_id is a specific issue key
        if "-" in target_id and not target_id.lower().startswith("plan-"):
            try:
                root_issue = jira_client.get_issue(target_id, bearer_token=token)
                tree = jira_client.recursively_build_subtree(issue=root_issue, bearer_token=token)
                return json.dumps({
                    "plan_or_root_id": target_id,
                    "summary": tree["summary"],
                    "type": tree["type"],
                    "status": tree["status"],
                    "metrics": tree["metrics"],
                    "critical_blockers_count": len(tree["blockers"]),
                    "critical_blockers": tree["blockers"],
                    "hierarchy": tree
                }, indent=2)
            except JiraNotFoundError:
                pass

        # Otherwise treat target_id as project or plan name/key
        return traverse_hierarchy(issue_keys="all", project_key=target_id, include_full_tree=True)

    except JiraAuthError as e:
        return json.dumps({"error": "Authentication Failed", "details": str(e)}, indent=2)
    except Exception as e:
        logger.error(f"Error in get_plan_hierarchy: {e}")
        return json.dumps({"error": f"Failed to get hierarchy for '{plan_id}'", "details": str(e)}, indent=2)


@mcp_server.tool(
    name="get_all_leaf_issues",
    description="Get the full status of all lowest leaf sub-tasks and stories across one or multiple live Jira Plans or Projects. Supports optional filtering by plan_id, status (e.g. BLOCKED, DONE, In Progress), or assignee in structured JSON."
)
def get_all_leaf_issues(
    plan_id: Optional[str] = "all",
    status_filter: Optional[str] = None,
    assignee_filter: Optional[str] = None
) -> str:
    """Aggregates all live leaf sub-tasks across projects with full lineage in structured JSON."""
    try:
        target = plan_id.strip() if plan_id and plan_id.lower() not in ("all", "*", "") else None
        if target and "-" in target and not target.lower().startswith("plan-"):
            traversal_result_str = traverse_hierarchy(issue_keys=target, project_key=None, include_full_tree=True)
        else:
            traversal_result_str = traverse_hierarchy(issue_keys="all", project_key=target, include_full_tree=True)
        traversal = json.loads(traversal_result_str)

        if "error" in traversal:
            return traversal_result_str

        all_leaves = []
        structures = traversal.get("traversed_structures", [])
        for s in structures:
            def extract_leaves(node):
                if not node.get("children"):
                    is_blk = bool(node.get("is_blocked") or "BLOCK" in (node.get("status") or "").upper())
                    all_leaves.append({
                        "subtask_key": node.get("root_key") or node.get("node_key") or node.get("leaf_key"),
                        "summary": node.get("summary"),
                        "type": node.get("type"),
                        "status": node.get("status"),
                        "assignee": node.get("assignee"),
                        "is_blocked": is_blk,
                        "blocker_reason": node.get("blocker_reason")
                    })
                else:
                    for child in node.get("children", []):
                        extract_leaves(child)

            extract_leaves(s)

        # Apply filters
        filtered = []
        for leaf in all_leaves:
            if status_filter and status_filter.lower() not in ("all", ""):
                if status_filter.lower() not in (leaf.get("status") or "").lower():
                    continue
            if assignee_filter and assignee_filter.lower() not in ("all", ""):
                if assignee_filter.lower() not in (leaf.get("assignee") or "").lower():
                    continue
            filtered.append(leaf)

        total = len(filtered)
        done_count = sum(1 for i in filtered if (i.get("status") or "").upper() in ("DONE", "RESOLVED", "CLOSED"))
        blocked_count = sum(1 for i in filtered if i.get("is_blocked"))
        in_prog_count = sum(1 for i in filtered if (i.get("status") or "").upper() in ("IN PROGRESS", "ACTIVE"))
        todo_count = total - done_count - blocked_count - in_prog_count

        payload = {
            "filter_applied": {
                "plan_id": plan_id or "all",
                "status_filter": status_filter or "all",
                "assignee_filter": assignee_filter or "all"
            },
            "summary_metrics": {
                "total_leaf_issues": total,
                "done": done_count,
                "in_progress": in_prog_count,
                "blocked": blocked_count,
                "todo": todo_count,
                "progress_pct": round((done_count / total * 100.0), 1) if total > 0 else 0.0
            },
            "leaf_issues": filtered
        }
        return json.dumps(payload, indent=2)

    except Exception as e:
        logger.error(f"Error in get_all_leaf_issues: {e}")
        return json.dumps({"error": "Failed to aggregate live leaf issues", "details": str(e)}, indent=2)


@mcp_server.tool(
    name="get_issue_descendants",
    description="Drill down into any specific live Jira Issue, Initiative, or Epic key to inspect all its child stories, tasks, and lowest leaf subtasks in structured JSON."
)
def get_issue_descendants(issue_key: str) -> str:
    """Drills down into a specific live Jira issue key to inspect its child subtree."""
    token = get_current_bearer_token()
    try:
        issue = jira_client.get_issue(issue_key.strip(), bearer_token=token)
        tree = jira_client.recursively_build_subtree(issue=issue, bearer_token=token)
        return json.dumps(tree, indent=2)
    except JiraNotFoundError as e:
        return json.dumps({"error": "Issue Not Found", "details": str(e)}, indent=2)
    except JiraAuthError as e:
        return json.dumps({"error": "Authentication Failed", "details": str(e)}, indent=2)
    except Exception as e:
        logger.error(f"Error in get_issue_descendants: {e}")
        return json.dumps({"error": f"Failed to get descendants for '{issue_key}'", "details": str(e)}, indent=2)


@mcp_server.tool(
    name="get_leaf_blockers",
    description="Discover and list all blocked leaf-level sub-tasks in a live Jira Plan or Project on the critical path, including assignee and blocker reasons in structured JSON."
)
def get_leaf_blockers(plan_id: str) -> str:
    """Finds all blocked leaf sub-tasks in a live Jira Plan or Project in structured JSON."""
    try:
        target = plan_id.strip() if plan_id and plan_id.lower() not in ("all", "*", "") else None
        if target and "-" in target and not target.lower().startswith("plan-"):
            traversal_result_str = traverse_hierarchy(issue_keys=target, project_key=None, include_full_tree=True)
        else:
            traversal_result_str = traverse_hierarchy(issue_keys="all", project_key=target, include_full_tree=True)
        traversal = json.loads(traversal_result_str)

        if "error" in traversal:
            return traversal_result_str

        blockers = traversal.get("critical_blockers", [])
        return json.dumps({
            "plan_or_project_id": plan_id,
            "total_blockers": len(blockers),
            "blockers": blockers
        }, indent=2)
    except Exception as e:
        logger.error(f"Error in get_leaf_blockers: {e}")
        return json.dumps({"error": f"Failed to get blockers for '{plan_id}'", "details": str(e)}, indent=2)


# ============================================================================
# Unified Dual Transport Setup (Streamable HTTP + SSE + REST)
# ============================================================================

sec_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)

streamable_session_mgr = StreamableHTTPSessionManager(
    app=mcp_server._lowlevel_server,
    security_settings=sec_settings
)
mcp_server._session_manager = streamable_session_mgr
streamable_app = StreamableHTTPASGIApp(streamable_session_mgr)

sse_app_instance = mcp_server.sse_app(transport_security=sec_settings)


class DualProtocolHandler:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["method"] in ("POST", "DELETE"):
                return await streamable_app(scope, receive, send)
            headers = dict(scope.get("headers", []))
            accept = headers.get(b"accept", b"").decode().lower()
            if "text/event-stream" in accept and b"session_id" not in scope.get("query_string", b""):
                return await sse_app_instance(scope, receive, send)
            return await streamable_app(scope, receive, send)
        return await streamable_app(scope, receive, send)


dual_handler = DualProtocolHandler()


# REST Endpoints
async def root_overview(request: Request):
    return JSONResponse({
        "service": "Live Jira Plans Hierarchy MCP Server",
        "status": "online",
        "mode": "live_atlassian_cloud",
        "mock_data": False,
        "mcp_endpoints": ["/sse", "/mcp", "/"],
        "transports_supported": ["streamable_http", "sse"],
        "tools": [
            "traverse_hierarchy",
            "list_plans",
            "get_plan_hierarchy",
            "get_all_leaf_issues",
            "get_issue_descendants",
            "get_leaf_blockers"
        ]
    })


async def healthz_check(request: Request):
    return JSONResponse({
        "status": "healthy",
        "service": "jira-plans-mcp",
        "mode": "live_atlassian_cloud"
    })


async def api_traverse(request: Request):
    keys = request.query_params.get("keys", "all")
    project = request.query_params.get("project")
    return JSONResponse(json.loads(traverse_hierarchy(issue_keys=keys, project_key=project)))


async def api_list_plans(request: Request):
    return JSONResponse(json.loads(list_plans()))


async def api_get_hierarchy(request: Request):
    plan_id = request.path_params["plan_id"]
    return JSONResponse(json.loads(get_plan_hierarchy(plan_id)))


async def api_get_leaf_issues(request: Request):
    plan_id = request.query_params.get("plan_id", "all")
    status = request.query_params.get("status")
    assignee = request.query_params.get("assignee")
    return JSONResponse(json.loads(get_all_leaf_issues(plan_id, status, assignee)))


async def api_get_descendants(request: Request):
    issue_key = request.path_params["issue_key"]
    return JSONResponse(json.loads(get_issue_descendants(issue_key)))


async def api_get_blockers(request: Request):
    plan_id = request.path_params["plan_id"]
    return JSONResponse(json.loads(get_leaf_blockers(plan_id)))


# Unified Starlette Application Routing
routes = [
    Route("/sse", endpoint=dual_handler, methods=["GET", "POST", "HEAD", "OPTIONS", "DELETE"]),
    Route("/mcp", endpoint=dual_handler, methods=["GET", "POST", "HEAD", "OPTIONS", "DELETE"]),
    Mount("/messages", app=sse_app_instance),
    Route("/healthz", endpoint=healthz_check, methods=["GET"]),
    Route("/api/traverse", endpoint=api_traverse, methods=["GET"]),
    Route("/api/plans", endpoint=api_list_plans, methods=["GET"]),
    Route("/api/plans/{plan_id}/hierarchy", endpoint=api_get_hierarchy, methods=["GET"]),
    Route("/api/plans/{plan_id}/blockers", endpoint=api_get_blockers, methods=["GET"]),
    Route("/api/leaves", endpoint=api_get_leaf_issues, methods=["GET"]),
    Route("/api/issues/{issue_key}/descendants", endpoint=api_get_descendants, methods=["GET"]),
    Route("/", endpoint=root_overview, methods=["GET"])
]


# Middleware to intercept and store Authorization: Bearer token from Gemini Enterprise
class AuthTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_val = headers.get(b"authorization")
            auth_str = auth_val.decode("utf-8") if auth_val else None
            token_reset = auth_token_cv.set(auth_str)
            try:
                return await self.app(scope, receive, send)
            finally:
                auth_token_cv.reset(token_reset)
        return await self.app(scope, receive, send)


base_app = Starlette(
    routes=routes,
    lifespan=lambda a: streamable_session_mgr.run()
)

app = AuthTokenMiddleware(base_app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 Starting Live Jira Plans MCP Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
