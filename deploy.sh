#!/usr/bin/env bash
# ==============================================================================
# 🚀 Jira Plans & Hierarchy MCP Server - Turnkey Cloud Run Deployment Script
# Deploys the BYO Custom MCP Connector for Gemini Enterprise (Discovery Engine)
# ==============================================================================
set -euo pipefail

# Color helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

usage() {
    cat <<EOF
Usage: ./deploy.sh [PROJECT_ID] [REGION] [OPTIONS]

Deploys the stateless Jira Plans & Hierarchy MCP Server to Google Cloud Run for
use as a Bring-Your-Own (BYO) Custom MCP Connector in Gemini Enterprise.

Positional Arguments (optional if flags or gcloud defaults are used):
  PROJECT_ID               Google Cloud Project ID
  REGION                   Google Cloud Region (default: us-central1)

Options:
  -p, --project ID         Google Cloud Project ID
  -r, --region REGION      Cloud Run region (default: us-central1)
  -s, --service NAME       Cloud Run service name (default: jira-plans-mcp)
      --skip-tests         Skip local pre-flight unit tests
  -h, --help               Show this help message and exit

Examples:
  ./deploy.sh my-gcp-project
  ./deploy.sh my-gcp-project us-central1
  ./deploy.sh --project my-gcp-project --region us-east1
EOF
}

# Parse CLI flags & positional arguments
POS_ARGS=()
SKIP_TESTS=false
CLI_PROJECT=""
CLI_REGION=""
CLI_SERVICE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -p|--project)
            CLI_PROJECT="${2:-}"
            shift 2
            ;;
        -r|--region)
            CLI_REGION="${2:-}"
            shift 2
            ;;
        -s|--service)
            CLI_SERVICE="${2:-}"
            shift 2
            ;;
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        -*)
            echo -e "${RED}❌ Unknown option: $1${NC}"
            usage
            exit 1
            ;;
        *)
            POS_ARGS+=("$1")
            shift
            ;;
    esac
done

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}  🚀 Jira Plans & Hierarchy MCP Server Deployment (Gemini Enterprise) ${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 0. Verify gcloud CLI & active authentication
if ! command -v gcloud &>/dev/null; then
    echo -e "${RED}❌ Error: Google Cloud SDK ('gcloud') is not installed or not in PATH.${NC}"
    echo "Install it from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

ACTIVE_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -n 1 || true)
if [ -z "$ACTIVE_ACCOUNT" ]; then
    echo -e "${RED}❌ Error: No active gcloud account authenticated.${NC}"
    echo -e "Please run: ${CYAN}gcloud auth login${NC}"
    exit 1
fi

# 1. Resolve GCP Project, Region, and Service Name
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
if [ "$CURRENT_PROJECT" = "(unset)" ]; then
    CURRENT_PROJECT=""
fi

PROJECT_ID="${CLI_PROJECT:-${POS_ARGS[0]:-${PROJECT_ID:-$CURRENT_PROJECT}}}"
REGION="${CLI_REGION:-${POS_ARGS[1]:-${REGION:-us-central1}}}"
SERVICE_NAME="${CLI_SERVICE:-${SERVICE_NAME:-jira-plans-mcp}}"

# Interactive fallback if PROJECT_ID is still empty and running in a TTY
if [ -z "$PROJECT_ID" ] && [ -t 0 ]; then
    echo -ne "${YELLOW}Enter your Google Cloud Project ID: ${NC}"
    read -r PROJECT_ID
fi

if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}❌ Error: No Google Cloud Project ID specified.${NC}"
    echo -e "Run: ${CYAN}./deploy.sh <YOUR_GCP_PROJECT_ID> [REGION]${NC}"
    exit 1
fi

echo -e "\n📋 ${YELLOW}Deployment Configuration:${NC}"
echo -e "  • Active Account : ${GREEN}${ACTIVE_ACCOUNT}${NC}"
echo -e "  • Project ID     : ${GREEN}${PROJECT_ID}${NC}"
echo -e "  • Region         : ${GREEN}${REGION}${NC}"
echo -e "  • Service Name   : ${GREEN}${SERVICE_NAME}${NC}"
echo -e "  • Jira Auth Mode : ${GREEN}Dynamic 3LO OAuth Pass-Through (Stateless)${NC}"

# Verify project access early
if ! PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)" 2>/dev/null); then
    echo -e "\n${RED}❌ Error: Cannot access project '${PROJECT_ID}'. Verify the Project ID and your IAM permissions.${NC}"
    exit 1
fi

# 2. Run Pre-flight Unit Tests (auto-bootstraps .venv if needed)
if [ "$SKIP_TESTS" = false ]; then
    echo -e "\n🧪 ${YELLOW}Step 1: Running Pre-flight Unit Tests...${NC}"
    PY_BIN=""
    if [ -x ".venv/bin/python3" ] && .venv/bin/python3 -c "import mcp, httpx, starlette" &>/dev/null; then
        PY_BIN=".venv/bin/python3"
    elif command -v python3 &>/dev/null && python3 -c "import mcp, httpx, starlette" &>/dev/null; then
        PY_BIN="python3"
    elif command -v python3 &>/dev/null && python3 -m venv --help &>/dev/null; then
        echo -e "  📦 Bootstrapping local virtual environment (.venv) for pre-flight tests..."
        if python3 -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -r requirements.txt; then
            PY_BIN=".venv/bin/python3"
        else
            echo -e "${YELLOW}  ⚠️ Could not auto-install test dependencies locally; Cloud Build will install from requirements.txt.${NC}"
        fi
    fi

    if [ -n "$PY_BIN" ]; then
        if $PY_BIN -m unittest test_mcp.py; then
            echo -e "${GREEN}✅ All unit tests passed successfully.${NC}"
        else
            echo -e "${RED}❌ Unit tests failed! Aborting deployment.${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}⚠️ Local Python test environment unavailable, proceeding to Cloud Build.${NC}"
    fi
else
    echo -e "\n⏭️  ${YELLOW}Step 1: Skipping local pre-flight unit tests (--skip-tests).${NC}"
fi

# 3. Enable Required Google Cloud APIs
echo -e "\n🔧 ${YELLOW}Step 2: Enabling Required GCP APIs...${NC}"
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    orgpolicy.googleapis.com \
    --project="${PROJECT_ID}" --quiet

echo -e "${GREEN}✅ GCP APIs enabled.${NC}"

# 4. Configure Org Policy for Gemini Enterprise Custom MCP Connector
echo -e "\n🛡️ ${YELLOW}Step 3: Checking & Applying Discovery Engine Custom MCP Org Policy...${NC}"
TMP_POLICY=$(mktemp /tmp/policy_disable_mcp_constraint.XXXXXX.yaml)
trap 'rm -f "$TMP_POLICY"' EXIT

cat <<EOF > "$TMP_POLICY"
name: projects/${PROJECT_ID}/policies/discoveryengine.managed.disableCustomMcpServerConnector
spec:
  rules:
  - enforce: false
EOF

if gcloud org-policies set-policy "$TMP_POLICY" --project="${PROJECT_ID}" --quiet 2>/dev/null; then
    echo -e "${GREEN}✅ Custom MCP Server connector Org Policy configured successfully.${NC}"
else
    echo -e "${YELLOW}⚠️ Could not automatically update Org Policy (requires 'roles/orgpolicy.policyAdmin').${NC}"
    echo -e "${YELLOW}   If Gemini Enterprise blocks custom MCP creation, ask an Org Admin to disable:${NC}"
    echo -e "${CYAN}   constraints/discoveryengine.managed.disableCustomMcpServerConnector${NC}"
fi

# 5. Build and Deploy to Google Cloud Run
echo -e "\n🚀 ${YELLOW}Step 4: Building & Deploying Stateless Service to Google Cloud Run...${NC}"

gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --allow-unauthenticated \
    --timeout=300 \
    --set-env-vars "LOG_LEVEL=${LOG_LEVEL:-INFO}" \
    --quiet

# 6. Retrieve Service URL & Verify Live Endpoints
NUMERIC_URL="https://${SERVICE_NAME}-${PROJECT_NUM}.${REGION}.run.app"
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")

echo -e "\n🩺 ${YELLOW}Step 5: Verifying Cloud Run Service Health...${NC}"
HEALTH_STATUS=$(curl -fsSL "${NUMERIC_URL}/" 2>/dev/null || curl -fsSL "${SERVICE_URL}/" 2>/dev/null || python3 -c "import urllib.request, sys; sys.stdout.write(urllib.request.urlopen('${SERVICE_URL}/').read().decode())" 2>/dev/null || echo "")

if echo "$HEALTH_STATUS" | grep -q "live_atlassian_cloud"; then
    echo -e "${GREEN}✅ Cloud Run service is online, healthy, and operating in live_atlassian_cloud mode!${NC}"
else
    echo -e "${YELLOW}⚠️ Health check response: ${HEALTH_STATUS}${NC}"
fi

# 7. Print Final Setup Summary for Gemini Enterprise
echo -e "\n${BLUE}======================================================================${NC}"
echo -e "${GREEN}  🎉 DEPLOYMENT COMPLETE! ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "\n📋 ${BOLD}Gemini Enterprise BYO Custom MCP Connector Configuration:${NC}"
echo -e "  • Authentication Method     : ${GREEN}OAuth 2.0${NC}"
echo -e "  • MCP Server URL (Primary)  : ${GREEN}${NUMERIC_URL}/sse${NC}"
echo -e "  • MCP Server URL (Alt)      : ${GREEN}${SERVICE_URL}/sse${NC}"
echo -e "  • Authorization URL         : ${GREEN}https://auth.atlassian.com/authorize${NC}"
echo -e "  • Authorization Parameters  : ${GREEN}&audience=api.atlassian.com&prompt=consent${NC}"
echo -e "  • Token URL                 : ${GREEN}https://auth.atlassian.com/oauth/token${NC}"
echo -e "  • Scopes                    : ${GREEN}read:jira-work read:jira-user offline_access${NC}"
echo -e "  • PKCE Support              : ${GREEN}Enabled (ON)${NC}"
echo -e "\n💡 Copy and paste the values above into the Gemini Enterprise Admin Console"
echo -e "   along with your Atlassian OAuth 2.0 (3LO) App Client ID and Client Secret."
echo -e "${BLUE}======================================================================${NC}\n"
