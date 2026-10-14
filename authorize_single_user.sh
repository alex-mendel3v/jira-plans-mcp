#!/usr/bin/env bash
# ==============================================================================
# 🔒 Authorize Single User (Zero Trust Hardening)
# Locks down the MCP server to Discovery Engine + exactly ONE specified user.
# ==============================================================================
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

USER_EMAIL="$1"
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
PROJECT_ID="${2:-${PROJECT_ID:-$CURRENT_PROJECT}}"
REGION="${3:-${REGION:-us-central1}}"
SERVICE_NAME="${SERVICE_NAME:-jira-plans-mcp}"

if [ -z "$USER_EMAIL" ] || [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}❌ Usage: ./authorize_single_user.sh <USER_EMAIL> [PROJECT_ID] [REGION]${NC}"
    echo "Example: ./authorize_single_user.sh mary@customer.com my-gcp-project us-central1"
    exit 1
fi

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}  🔒 Locking Down MCP Server to Single User: ${GREEN}${USER_EMAIL}${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 1. Remove public 'allUsers' invoker from Cloud Run
echo -e "\n🛡️ ${YELLOW}Step 1: Removing public access (allUsers) from Cloud Run...${NC}"
gcloud run services remove-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="allUsers" \
    --role="roles/run.invoker" \
    --quiet 2>/dev/null || echo -e "${YELLOW}  (Public access was already removed or not present)${NC}"

# 2. Authorize Discovery Engine Service Agent
echo -e "\n🤖 ${YELLOW}Step 2: Authorizing Discovery Engine Service Agent...${NC}"
PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
DISCOVERY_ENGINE_SA="service-${PROJECT_NUM}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${DISCOVERY_ENGINE_SA}" \
    --role="roles/run.invoker" \
    --quiet

echo -e "${GREEN}✅ Discovery Engine service account authorized: ${DISCOVERY_ENGINE_SA}${NC}"

# 3. Grant Discovery Engine Project-Level Viewer to User
echo -e "\n👤 ${YELLOW}Step 3: Granting Discovery Engine access to ${USER_EMAIL}...${NC}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/discoveryengine.viewer" \
    --quiet

# 4. Grant Cloud Run Invoker to User
echo -e "\n🚀 ${YELLOW}Step 4: Granting Cloud Run Invoker to ${USER_EMAIL}...${NC}"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/run.invoker" \
    --quiet

echo -e "\n${BLUE}======================================================================${NC}"
echo -e "${GREEN}  🎉 ACCESS RESTRICTED SUCCESSFULLY! ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "  • Authorized User          : ${GREEN}${USER_EMAIL}${NC}"
echo -e "  • Discovery Engine Service : ${GREEN}${DISCOVERY_ENGINE_SA}${NC}"
echo -e "  • Public Invocation        : ${RED}BLOCKED (403 Forbidden)${NC}"
echo -e "${BLUE}======================================================================${NC}\n"
