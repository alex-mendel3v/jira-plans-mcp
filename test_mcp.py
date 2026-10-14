import json
import time
import unittest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient

import server
from server import app, auth_token_cv
from jira_client import JiraClient, JiraAuthError, JiraNotFoundError, sanitize_jql_value


class TestJiraPlansMCPServer(unittest.TestCase):
    """Automated unit and protocol test suite for Jira Plans & Hierarchy MCP Server."""

    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def setUp(self):
        # Clear cached cloud IDs before each test
        server.jira_client._cloud_id_cache.clear()

    def _seed_test_token_cloud_id(self, token: str = "test-oauth-token", cloud_id: str = "test-cloud-id-123"):
        server.jira_client._set_cached_cloud_id(token, cloud_id)

    def _get_mock_hierarchy_issues(self):
        return {
            "EX-1": {
                "key": "EX-1",
                "fields": {
                    "summary": "Core Platform Initiative",
                    "status": {"name": "In Progress"},
                    "issuetype": {"name": "Epic"},
                    "assignee": {"displayName": "Alex Rivera"},
                    "subtasks": [],
                    "issuelinks": []
                }
            },
            "EX-2": {
                "key": "EX-2",
                "fields": {
                    "summary": "Service Upgrade Rollout",
                    "status": {"name": "In Progress"},
                    "issuetype": {"name": "Story"},
                    "assignee": {"displayName": "Jordan Lee"},
                    "parent": {"key": "EX-1"},
                    "subtasks": [
                        {
                            "key": "EX-3",
                            "fields": {
                                "summary": "Database Schema Migration",
                                "status": {"name": "Done"},
                                "issuetype": {"name": "Sub-task"},
                                "assignee": {"displayName": "Taylor Kim"},
                                "subtasks": [],
                                "issuelinks": []
                            }
                        },
                        {
                            "key": "EX-4",
                            "fields": {
                                "summary": "Provision Regional Cluster",
                                "status": {"name": "Blocked"},
                                "issuetype": {"name": "Sub-task"},
                                "assignee": {"displayName": "Morgan Chen"},
                                "subtasks": [],
                                "issuelinks": [
                                    {
                                        "type": {"inward": "is blocked by"},
                                        "inwardIssue": {
                                            "key": "EX-99",
                                            "fields": {"summary": "Awaiting Network Firewall Approval"}
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "issuelinks": []
                }
            }
        }

    def test_root_and_healthz_endpoints(self):
        """Verify service overview and health check endpoints."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["mode"], "live_atlassian_cloud")
        self.assertFalse(data["mock_data"])
        self.assertIn("traverse_hierarchy", data["tools"])

        health_res = self.client.get("/healthz")
        self.assertEqual(health_res.status_code, 200)
        self.assertEqual(health_res.json()["status"], "healthy")

    def test_mcp_streamable_http_initialize_and_list_tools(self):
        """Verify MCP JSON-RPC 2.0 initialize and tools/list over Streamable HTTP."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gemini-enterprise-test", "version": "1.0"}
            }
        }
        res = self.client.post("/sse", json=init_payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        session_id = res.headers.get("mcp-session-id")

        req_headers = dict(headers)
        if session_id:
            req_headers["mcp-session-id"] = session_id

        tools_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        res_tools = self.client.post("/sse", json=tools_payload, headers=req_headers)
        self.assertEqual(res_tools.status_code, 200)
        body = res_tools.text
        for tool_name in (
            "traverse_hierarchy",
            "list_plans",
            "get_plan_hierarchy",
            "get_all_leaf_issues",
            "get_issue_descendants",
            "get_leaf_blockers"
        ):
            self.assertIn(tool_name, body)

    def test_missing_oauth_token_returns_auth_error(self):
        """Verify that calling Jira endpoints without an OAuth Bearer token returns an Authentication Failed error."""
        res = self.client.get("/api/plans")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("error"), "Authentication Failed")
        self.assertIn("OAuth 2.0 Bearer token", data.get("details", ""))

    @patch("httpx.Client.get")
    def test_dynamic_cloud_id_discovery_and_hashed_cache(self, mock_get):
        """Verify 3LO OAuth dynamic site discovery and ensure raw tokens are never stored in memory."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"id": "discovered-cloud-id-999", "name": "test-tenant", "url": "https://test-tenant.atlassian.net"}
        ]
        mock_get.return_value = mock_resp

        jc = JiraClient()
        raw_token = "secret-user-oauth-access-token"
        headers, base_url = jc._get_auth_headers_and_base_url(f"Bearer {raw_token}")

        self.assertEqual(base_url, "https://api.atlassian.com/ex/jira/discovered-cloud-id-999")
        self.assertEqual(headers["Authorization"], f"Bearer {raw_token}")
        # Verify raw token is NOT a key in _cloud_id_cache
        self.assertNotIn(raw_token, jc._cloud_id_cache)
        self.assertEqual(len(jc._cloud_id_cache), 1)

        # Second call should hit cache without making another HTTP call
        mock_get.reset_mock()
        _, base_url_2 = jc._get_auth_headers_and_base_url(raw_token)
        self.assertEqual(base_url_2, "https://api.atlassian.com/ex/jira/discovered-cloud-id-999")
        mock_get.assert_not_called()

        # Verify TTL expiration evicts expired entry
        token_hash = jc._hash_token(raw_token)
        jc._cloud_id_cache[token_hash] = ("discovered-cloud-id-999", time.time() - 10)
        self.assertIsNone(jc._get_cached_cloud_id(raw_token))

    def test_auth_token_middleware_resets_contextvar(self):
        """Verify AuthTokenMiddleware propagates Authorization header during request and resets it afterwards."""
        self.assertIsNone(auth_token_cv.get())
        with patch.object(server.jira_client, "list_plans", return_value=[]) as mock_list:
            res = self.client.get("/api/plans", headers={"Authorization": "Bearer user-session-token-xyz"})
            self.assertEqual(res.status_code, 200)
            mock_list.assert_called_once_with(bearer_token="Bearer user-session-token-xyz")
        # ContextVar must be reset to None after request finishes
        self.assertIsNone(auth_token_cv.get())

    @patch.object(server.jira_client, "list_plans")
    def test_list_plans(self, mock_list_plans):
        """Verify /api/plans returns live plans structure."""
        mock_list_plans.return_value = [
            {
                "plan_id": "101",
                "name": "Q4 Architecture Roadmap",
                "lead": "Alex Rivera",
                "target_start": "2026-01-01",
                "target_end": "2026-12-31",
                "status": "ACTIVE",
                "is_native_plan": True
            }
        ]
        res = self.client.get("/api/plans", headers={"Authorization": "Bearer test-oauth-token"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total_plans"], 1)
        self.assertEqual(data["plans"][0]["plan_id"], "101")
        self.assertEqual(data["source"], "live_atlassian_cloud")

    @patch.object(server.jira_client, "get_plan")
    @patch.object(server.jira_client, "search_issues")
    @patch.object(server.jira_client, "get_child_issues")
    @patch.object(server.jira_client, "get_issue")
    def test_hierarchy_and_blockers_traversal(self, mock_get_issue, mock_get_children, mock_search, mock_get_plan):
        """Verify recursive hierarchy traversal, leaf rollup math, and critical blocker extraction."""
        issues_db = self._get_mock_hierarchy_issues()
        mock_get_plan.return_value = None
        mock_search.return_value = [issues_db["EX-1"]]

        def side_effect_children(parent_key, bearer_token=None):
            if parent_key == "EX-1":
                return [issues_db["EX-2"]]
            return []

        def side_effect_issue(key, bearer_token=None):
            if key in issues_db:
                return issues_db[key]
            raise JiraNotFoundError(f"Not found: {key}")

        mock_get_children.side_effect = side_effect_children
        mock_get_issue.side_effect = side_effect_issue

        auth_headers = {"Authorization": "Bearer test-oauth-token"}

        # 1. Test Plan/Project Hierarchy
        res = self.client.get("/api/plans/EX/hierarchy", headers=auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("summary_metrics", data)
        metrics = data["summary_metrics"]
        self.assertEqual(metrics["total_leaf_subtasks"], 2)
        self.assertEqual(metrics["completed_leaves"], 1)
        self.assertEqual(metrics["blocked_leaves"], 1)
        self.assertEqual(metrics["overall_progress_pct"], 50.0)

        # 2. Test Blockers Endpoint
        res_blk = self.client.get("/api/plans/EX/blockers", headers=auth_headers)
        self.assertEqual(res_blk.status_code, 200)
        blk_data = res_blk.json()
        self.assertEqual(blk_data["total_blockers"], 1)
        self.assertEqual(blk_data["blockers"][0]["leaf_key"], "EX-4")
        self.assertIn("EX-99", blk_data["blockers"][0]["blocker_reason"])

        # 3. Test Leaf Issues with Status Filter
        res_leaves = self.client.get("/api/leaves?plan_id=EX&status=Blocked", headers=auth_headers)
        self.assertEqual(res_leaves.status_code, 200)
        leaves_data = res_leaves.json()
        self.assertEqual(leaves_data["summary_metrics"]["total_leaf_issues"], 1)
        self.assertEqual(leaves_data["leaf_issues"][0]["subtask_key"], "EX-4")

        # 4. Test Descendants Drilldown
        res_desc = self.client.get("/api/issues/EX-1/descendants", headers=auth_headers)
        self.assertEqual(res_desc.status_code, 200)
        desc_data = res_desc.json()
        self.assertEqual(desc_data["node_key"], "EX-1")
        self.assertEqual(desc_data["metrics"]["total_leaves"], 2)

        # 5. Test Universal Traverse Endpoint
        res_trav = self.client.get("/api/traverse?keys=EX-1", headers=auth_headers)
        self.assertEqual(res_trav.status_code, 200)
        trav_data = res_trav.json()
        self.assertEqual(trav_data["summary_metrics"]["total_leaf_subtasks"], 2)

    def test_cycle_protection_in_recursive_subtree(self):
        """Verify circular parent-child references do not cause infinite recursion."""
        self._seed_test_token_cloud_id("cycle-token", "cloud-1")
        issue_a = {
            "key": "CYC-1",
            "fields": {
                "summary": "Node A",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Epic"},
                "subtasks": [{"key": "CYC-2", "fields": {"summary": "Node B", "status": {"name": "To Do"}, "issuetype": {"name": "Task"}, "subtasks": [{"key": "CYC-1"}]}}]
            }
        }
        tree = server.jira_client.recursively_build_subtree(issue_a, bearer_token="cycle-token")
        self.assertEqual(tree["node_key"], "CYC-1")
        self.assertEqual(tree["metrics"]["total_leaves"], 1)
        self.assertEqual(tree["leaves"][0]["leaf_key"], "CYC-2")

    @patch("httpx.Client.post")
    def test_live_jira_client_search_jql(self, mock_post):
        """Verify JiraClient.search_issues calls the modern /rest/api/3/search/jql endpoint."""
        self._seed_test_token_cloud_id("test-token", "cloud-abc")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "issues": [
                {
                    "key": "EX-10",
                    "fields": {
                        "summary": "Live Test Issue",
                        "status": {"name": "Done"},
                        "issuetype": {"name": "Task"}
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        issues = server.jira_client.search_issues('project = "EX"', bearer_token="test-token")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["key"], "EX-10")
        called_url = mock_post.call_args[0][0]
        self.assertTrue(called_url.endswith("/rest/api/3/search/jql"))

    @patch("httpx.Client.get")
    def test_retry_on_429_rate_limit(self, mock_get):
        """Verify JiraClient automatically retries on HTTP 429 Too Many Requests."""
        self._seed_test_token_cloud_id("retry-token", "cloud-retry")
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"key": "EX-1", "fields": {"summary": "Recovered after 429"}}

        mock_get.side_effect = [resp_429, resp_200]
        issue = server.jira_client.get_issue("EX-1", bearer_token="retry-token")
        self.assertEqual(issue["key"], "EX-1")
        self.assertEqual(mock_get.call_count, 2)

    def test_jql_input_sanitization(self):
        """Verify sanitize_jql_value escapes quotes and backslashes while preserving standard keys."""
        self.assertEqual(sanitize_jql_value("EX-123"), "EX-123")
        self.assertEqual(sanitize_jql_value('BAD"KEY'), 'BAD\\"KEY')


if __name__ == "__main__":
    unittest.main()
