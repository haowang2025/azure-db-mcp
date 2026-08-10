# News Intelligence Agent B PostgreSQL MCP

This repository builds a Docker image for Azure Container Apps. The container exposes a Streamable HTTP MCP endpoint that gives Agent B controlled read/write access to the `news_agent` PostgreSQL schema.

## Architecture

```text
Deepnote App / NewsAnalysisService
            |
            | creates PID / RID / JOB_ID
            v
Azure PostgreSQL (news_agent)
            ^
            | MCP read/write
            |
Agent B Harness -> Azure Container Apps -> this MCP container
```

The MCP endpoint is `/mcp` on port `8000` by default.

## Security boundary

This MCP is **not read-only**, but it intentionally does **not** expose arbitrary SQL.

Agent B can:

- read news, reports, jobs, subjects, relations, evidence, and workflow context;
- mark its own jobs running;
- update heartbeat, progress, stage, and allowed active statuses;
- finalize the RID requested by its JOB_ID;
- mark failures/retries through the existing database functions;
- append/update evidence for an existing relation.

Agent B cannot through this MCP:

- run arbitrary SQL;
- create/drop/alter database objects;
- delete rows;
- create arbitrary jobs or report versions;
- create or redefine canonical subjects/relations.

## Domain tools

### Database / schema

- `db_status()`
- `system_overview()`
- `list_tables(schema="news_agent")`
- `describe_table(table, schema="news_agent")`
- `preview_table(table, limit=20, schema="news_agent")`

### News / reports / jobs

- `get_news(pid)`
- `search_news(query)`
- `list_report_versions(pid)`
- `get_latest_report(pid, completed_only=True)`
- `get_report(rid)`
- `get_job(job_id)`
- `get_jobs_for_pid(pid)`
- `list_active_jobs()`
- `get_agent_b_context(job_id)`

`get_agent_b_context(job_id)` is the preferred Agent B entry point. It returns JOB_ID/PID/RID, source news, canonical subject, Agent A result, current report version, and known relations.

### Subjects / relations

- `get_news_mentions(pid)`
- `search_subjects(query)`
- `get_subject_context(subject_id)`
- `search_relations(query)`
- `get_relation_evidence(relation_id)`
- `list_relation_types()`

### Agent B writes

- `mark_job_running(job_id, ...)`
- `heartbeat_agent_b_job(job_id, ...)`
- `update_job_progress(job_id, ...)`
- `complete_agent_b_job(job_id, report_markdown, ...)`
- `fail_agent_b_job(job_id, error_code, error_message, retryable=False, ...)`
- `add_relation_evidence(relation_id, pid, evidence_text, ...)`

## Typical Agent B flow

```text
Receive JOB_ID
   |
   v
get_agent_b_context(JOB_ID)
   |
   v
mark_job_running(...)
   |
   +--> heartbeat_agent_b_job / update_job_progress
   |
   +--> Web / Wind / subagent research
   |
   +--> optional add_relation_evidence(...)
   |
   +--> complete_agent_b_job(...)
        or fail_agent_b_job(...)
```

`complete_agent_b_job()` calls the existing `news_agent.finalize_report()` function. The database updates the requested RID, JOB_ID, and PID consistently. `report_markdown` is stored in PostgreSQL; `report_path` and `version_report_path` are left `NULL`, because Agent B may run outside Deepnote.

## 1. Configure the PostgreSQL Agent B role

Run the included SQL as the Azure PostgreSQL database owner/admin:

```bash
psql "host=YOUR_SERVER.postgres.database.azure.com port=5432 dbname=YOUR_DB user=YOUR_ADMIN sslmode=require" \
  -v mcp_password='USE_A_LONG_RANDOM_PASSWORD' \
  -f db/setup_agent_b.sql
```

The script creates/configures `news_agent_b_mcp` with:

- `SELECT` access to the `news_agent` schema;
- column-level workflow/report UPDATE privileges needed by the existing SECURITY INVOKER functions;
- INSERT/UPDATE privileges on `relation_evidence` only;
- EXECUTE privileges for `new_id`, `set_job_status`, `finalize_report`, and `fail_job`.

Do not use the Azure PostgreSQL administrator account in the container.

## 2. Local Docker test

Copy `.env.example` to `.env` and fill in database values.

```bash
docker build -t news-agent-b-pg-mcp .

docker run --rm -p 8000:8000 \
  --env-file .env \
  news-agent-b-pg-mcp
```

MCP endpoint:

```text
http://localhost:8000/mcp
```

## 3. GitHub Container Registry

GitHub Actions builds and pushes:

```text
ghcr.io/haowang2025/azure-pg-mcp:latest
ghcr.io/haowang2025/azure-pg-mcp:<commit-sha>
```

The workflow performs a Python syntax check, builds `linux/amd64`, pushes the image, starts the container, and verifies that port 8000 accepts connections.

Because this repository is private, GHCR may also be private. Configure Azure Container Apps registry credentials with `read:packages`, or make the package public if appropriate.

## 4. Azure Container Apps

Example deployment:

```bash
RG=pg-mcp
LOC=eastus
ENV=pg-mcp-env
APP=pg-mcp

az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App

az group create --name "$RG" --location "$LOC"

az containerapp env create \
  --name "$ENV" \
  --resource-group "$RG" \
  --location "$LOC" \
  --logs-destination none

az containerapp create \
  --name "$APP" \
  --resource-group "$RG" \
  --environment "$ENV" \
  --image ghcr.io/haowang2025/azure-pg-mcp:latest \
  --ingress external \
  --target-port 8000 \
  --cpu 0.25 \
  --memory 0.5Gi \
  --min-replicas 0 \
  --max-replicas 1
```

Store the PostgreSQL password as a Container Apps secret:

```bash
az containerapp secret set \
  --name "$APP" \
  --resource-group "$RG" \
  --secrets dbpassword='YOUR_AGENT_B_MCP_DB_PASSWORD'
```

Configure the container:

```bash
az containerapp update \
  --name "$APP" \
  --resource-group "$RG" \
  --set-env-vars \
    DBHOST=YOUR_SERVER.postgres.database.azure.com \
    DBPORT=5432 \
    DBNAME=YOUR_DB \
    DBUSER=news_agent_b_mcp \
    DBPASSWORD=secretref:dbpassword \
    SSLMODE=require \
    NEWS_SCHEMA=news_agent \
    ALLOWED_SCHEMAS=news_agent \
    DB_CONNECT_TIMEOUT=10 \
    DB_STATEMENT_TIMEOUT_MS=30000 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp
```

Get the public endpoint:

```bash
FQDN=$(az containerapp show \
  --name "$APP" \
  --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn \
  -o tsv)

echo "https://${FQDN}/mcp"
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `DBHOST` | yes | - | Azure PostgreSQL hostname |
| `DBPORT` | no | `5432` | PostgreSQL port |
| `DBNAME` | yes | - | Database name |
| `DBUSER` | yes | - | `news_agent_b_mcp` role |
| `DBPASSWORD` | yes | - | Role password |
| `SSLMODE` | no | `require` | PostgreSQL TLS mode |
| `NEWS_SCHEMA` | no | `news_agent` | Default project schema |
| `ALLOWED_SCHEMAS` | no | `news_agent` | Comma-separated schema allowlist |
| `DB_CONNECT_TIMEOUT` | no | `10` | Connection timeout seconds |
| `DB_STATEMENT_TIMEOUT_MS` | no | `30000` | Statement timeout milliseconds |
| `MCP_HOST` | no | `0.0.0.0` | Bind address |
| `MCP_PORT` | no | `8000` | MCP port |
| `MCP_PATH` | no | `/mcp` | Streamable HTTP path |

## Networking and authentication

The Azure Container App must be able to reach PostgreSQL on port 5432. Prefer VNet/private networking for production data.

The MCP endpoint itself currently has no application-level authentication. Because this server has controlled write capabilities, do **not** expose it broadly on the public internet without an authentication/private-network layer. For Agent B on a local machine or VPS, prefer private ingress, VPN/Tailscale, an authenticated reverse proxy, or equivalent access control.
