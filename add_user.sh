#!/usr/bin/env bash
# ==============================================================================
# 👥 Add Additional User to MCP Connector
# Grants Discovery Engine and Cloud Run Invoker access to an additional user.
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
    echo -e "${RED}❌ Usage: ./add_user.sh <USER_EMAIL> [PROJECT_ID] [REGION]${NC}"
    echo "Example: ./add_user.sh john@customer.com my-gcp-project us-central1"
    exit 1
fi

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}  👥 Adding Additional User to MCP Connector: ${GREEN}${USER_EMAIL}${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 1. Grant Discovery Engine Project-Level Viewer to User
echo -e "\n👤 ${YELLOW}Step 1: Granting Discovery Engine access to ${USER_EMAIL}...${NC}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/discoveryengine.viewer" \
    --quiet

# 2. Grant Cloud Run Invoker to User
echo -e "\n🚀 ${YELLOW}Step 2: Granting Cloud Run Invoker to ${USER_EMAIL}...${NC}"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/run.invoker" \
    --quiet

echo -e "\n${BLUE}======================================================================${NC}"
echo -e "${GREEN}  🎉 USER ADDED SUCCESSFULLY! ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "  • Newly Added User : ${GREEN}${USER_EMAIL}${NC}"
echo -e "  • Cloud Run Service : ${GREEN}${SERVICE_NAME}${NC}"
echo -e "${BLUE}======================================================================${NC}\n"
