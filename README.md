# Jira Plans & Multi-Tier Hierarchy MCP Connector

A stateless, production-ready **Model Context Protocol (MCP)** server built for **Google Cloud Run** and **Gemini Enterprise (Discovery Engine)** as a Bring-Your-Own (BYO) Custom MCP Connector.

Standard Jira tools only perform flat issue searches and lose context across parent-child relationships. This connector dynamically traverses arbitrary multi-tier Atlassian Cloud hierarchies (**Plan $\rightarrow$ Initiative $\rightarrow$ Epic $\rightarrow$ Story/Task $\rightarrow$ Sub-task**) down to terminal leaf items, computing real-time completion rollups and critical-path blocker lineage on the fly.

---

## Key Capabilities & Security Model

- **100% Dynamic & Stateless (3LO OAuth Pass-Through)**:
  - The server stores **zero** Jira URLs, project keys, credentials, or user data.
  - Every tool invocation is authenticated dynamically using the caller's **Atlassian OAuth 2.0 (3LO)** Bearer token forwarded by Gemini Enterprise in the `Authorization` header.
  - For each request, the connector dynamically discovers the authenticated user's accessible Atlassian Cloud site via `https://api.atlassian.com/oauth/token/accessible-resources` and routes queries to `https://api.atlassian.com/ex/jira/{cloud_id}`, strictly enforcing the user's native Jira permissions.
- **Dual MCP Transport Support**:
  - Simultaneously supports **Streamable HTTP** (`POST /sse`, `POST /mcp`) and **Server-Sent Events (SSE)** (`GET /sse`, `/messages`) for seamless compatibility with Gemini Enterprise tool discovery and execution.
- **6 Specialized Hierarchy & Rollup MCP Tools**:
  1. `traverse_hierarchy`: Universal recursive hierarchy traversal engine for any Jira issue keys, Plan IDs, or Project keys down to terminal leaf sub-tasks.
  2. `list_plans`: Discovers active Jira Plans (`/rest/api/3/plans/plan`) and accessible project roadmap containers.
  3. `get_plan_hierarchy`: Full multi-tier hierarchy breakdown of a Jira Plan or Project with bottom-up status rollups.
  4. `get_all_leaf_issues`: Aggregates lowest-level leaf stories and sub-tasks with optional filtering by plan/project, status, or assignee.
  5. `get_issue_descendants`: Drills down into any specific Initiative, Epic, or Story key to inspect its complete child subtree.
  6. `get_leaf_blockers`: Identifies all blocked leaf sub-tasks on the critical path along with their full ancestor lineage and blocking issue links.

---

## Quickstart: 3-Step Production Deployment

### Prerequisites
1. **Google Cloud SDK (`gcloud`)** installed and authenticated (`gcloud auth login`).
2. A **Google Cloud Project** where you have permissions to deploy Cloud Run services (`roles/run.admin`, `roles/cloudbuild.builds.editor`).
3. An **Atlassian Cloud** account to register an OAuth 2.0 (3LO) integration in the [Atlassian Developer Console](https://developer.atlassian.com/console/my-apps/).

---

### Step 1: Create Your Atlassian OAuth 2.0 (3LO) App

1. Open the [Atlassian Developer Console](https://developer.atlassian.com/console/my-apps/) and click **Create** $\rightarrow$ **OAuth 2.0 integration**.
2. Enter a name (e.g., `Gemini Enterprise Jira Plans MCP`) and create the app.
3. In the left navigation menu, go to **Permissions**:
   - Locate **Jira API** and click **Add** (or **Configure**).
   - Under **Classic scopes**, enable:
     - `read:jira-work`
     - `read:jira-user`
   - Save your changes.
4. In the left navigation menu, go to **Authorization**:
   - Next to **OAuth 2.0 (3LO)**, click **Add** (or **Configure**).
   - Set the **Callback URL** to the Gemini Enterprise OAuth redirect endpoint:
     ```text
     https://vertexaisearch.cloud.google.com/oauth-redirect
     ```
   - Click **Save changes**.
5. In the left navigation menu, go to **Settings** and copy your **Client ID** and **Secret** (you will paste these into Gemini Enterprise in Step 3).

---

### Step 2: Deploy to Google Cloud Run (`./deploy.sh`)

Clone this repository and run the turnkey deployment script:

```bash
git clone https://github.com/alex-mendel3v/jira-plans-mcp.git
cd jira-plans-mcp
chmod +x deploy.sh

# Deploy to your Google Cloud Project (defaults to us-central1 if region is omitted)
./deploy.sh <YOUR_GCP_PROJECT_ID> [REGION]
```

#### What `./deploy.sh` Automates:
1. **Pre-flight Validation**: Verifies `gcloud` authentication, validates access to your GCP project, and automatically bootstraps a local Python virtual environment (`.venv`) to run the offline unit test suite (`test_mcp.py`) before building.
2. **GCP API Enablement**: Enables Cloud Run (`run.googleapis.com`), Cloud Build (`cloudbuild.googleapis.com`), Artifact Registry (`artifactregistry.googleapis.com`), and Organization Policy (`orgpolicy.googleapis.com`) APIs.
3. **Custom MCP Org Policy**: Configures `constraints/discoveryengine.managed.disableCustomMcpServerConnector` (`enforce: false`) on the target project so Gemini Enterprise allows custom MCP registration.
4. **Cloud Run Build & Deployment**: Builds the container from source via Cloud Build and deploys the stateless service to Cloud Run.
5. **Health Check & Output**: Verifies the live `/healthz` and `/` endpoints and prints the exact configuration values to copy into Gemini Enterprise.

---

### Step 3: Register the BYO Custom MCP Connector in Gemini Enterprise

In the **Google Cloud Console**, navigate to **Gemini Enterprise (Agent Builder / Discovery Engine)** $\rightarrow$ **Data Stores / Connectors** $\rightarrow$ **Add Custom MCP Server**, and enter the values output by `./deploy.sh`:

| Field | Value |
| :--- | :--- |
| **MCP Server URL** | `https://jira-plans-mcp-<PROJECT_NUMBER>.<REGION>.run.app/sse` *(printed by `./deploy.sh`)* |
| **Authentication Type** | **OAuth 2.0** |
| **Client ID** | *Your Atlassian 3LO App Client ID (from Step 1)* |
| **Client Secret** | *Your Atlassian 3LO App Client Secret (from Step 1)* |
| **Authorization URL** | `https://auth.atlassian.com/authorize` |
| **Authorization Parameters** | `&audience=api.atlassian.com&prompt=consent` |
| **Token URL** | `https://auth.atlassian.com/oauth/token` |
| **Scopes** | `read:jira-work read:jira-user offline_access` |
| **PKCE Support** | **Enabled (ON)** |

---

## Optional IAM Lockdown Scripts

If you are deploying in an environment where Cloud Run ingress is restricted via GCP IAM (`--no-allow-unauthenticated`), two helper scripts are included:
- [`authorize_single_user.sh`](./authorize_single_user.sh): Removes public `allUsers` invoker access and binds `roles/run.invoker` strictly to your Discovery Engine Service Agent (`service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com`) and a single user email.
- [`add_user.sh`](./add_user.sh): Grants Discovery Engine Viewer and Cloud Run Invoker roles to additional users.

> **Note**: When Gemini Enterprise calls a Custom MCP Server configured with 3LO OAuth, it sends the user's **Atlassian OAuth Bearer token** in the `Authorization` HTTP header. Leaving `--allow-unauthenticated` enabled at the Cloud Run ingress layer (the default in `./deploy.sh`) ensures Cloud Run forwards the `Authorization: Bearer <atlassian_token>` header directly to the MCP container, which validates every token against Atlassian Cloud.

---

## Local Development & Testing

### Run the Automated Unit & Protocol Test Suite
The test suite in [`test_mcp.py`](./test_mcp.py) uses mocked HTTP responses (`unittest.mock`) to verify MCP protocol handshakes (`initialize`, `tools/list`), `Authorization: Bearer` context propagation, Atlassian API endpoint fallbacks, and recursive hierarchy rollup calculations without making external network calls:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python3 -m unittest -v test_mcp.py
```

### Run the Server Locally
```bash
./.venv/bin/python3 server.py
```
In a second terminal, verify the end-to-end SSE + JSON-RPC 2.0 handshake against the running server:
```bash
./.venv/bin/python3 test_full_mcp_handshake.py
```

---

## Repository Structure

| File | Description |
| :--- | :--- |
| [`server.py`](./server.py) | FastMCP + Starlette server exposing `/sse`, `/mcp`, `/messages`, `/healthz`, and REST endpoints with per-request `ContextVar` OAuth token isolation. |
| [`jira_client.py`](./jira_client.py) | Stateless Atlassian Cloud client handling 3LO site discovery (`accessible-resources`), modern JQL search (`/rest/api/3/search/jql`), and recursive subtree traversal. |
| [`deploy.sh`](./deploy.sh) | Turnkey Cloud Run deployment script with pre-flight unit tests, API enablement, Org Policy setup, and health verification. |
| [`openapi.yaml`](./openapi.yaml) | OpenAPI 3.0.3 specification for the REST endpoints and Atlassian 3LO OAuth 2.0 security scheme. |
| [`Dockerfile`](./Dockerfile) | Production Python 3.11 container image running as non-root `appuser` on port `8080`. |
| [`test_mcp.py`](./test_mcp.py) | Automated unit and MCP protocol test suite. |
| [`test_full_mcp_handshake.py`](./test_full_mcp_handshake.py) | Live SSE + JSON-RPC handshake verification script. |
