# Azure PostgreSQL Read-Only MCP

A small read-only MCP server intended for Azure Database for PostgreSQL Flexible Server and Azure Container Apps.

## Architecture

```text
ChatGPT Web
    |
    | MCP / HTTPS
    v
Azure Container Apps
    |
    | TLS PostgreSQL
    v
Azure Database for PostgreSQL Flexible Server
```

The MCP endpoint is `/mcp` on port `8000` by default.

## Exposed MCP tools

- `db_status()` - test connectivity and confirm read-only session settings
- `list_schemas()` - list non-system schemas
- `list_tables(schema="public")` - list tables and views
- `describe_table(table, schema="public")` - inspect columns
- `preview_table(table, limit=20, schema="public")` - read at most 100 rows

There is intentionally no arbitrary `run_sql` tool and no write tool.

## Security model

Use both layers:

1. PostgreSQL role `chatgpt_mcp` receives only connection/schema usage/SELECT privileges.
2. Every MCP connection sets `default_transaction_read_only=on` and a statement timeout.

Do not use a PostgreSQL administrator account for this service.

## 1. Configure the PostgreSQL read-only role

From a machine that can reach the Azure PostgreSQL server, run the included script as the database owner/admin:

```bash
psql "host=YOUR_SERVER.postgres.database.azure.com port=5432 dbname=YOUR_DB user=YOUR_ADMIN sslmode=require" \
  -v mcp_password='USE_A_LONG_RANDOM_PASSWORD' \
  -f db/setup_readonly.sql
```

The script configures `chatgpt_mcp` for the current database and the `public` schema.

If tables are created by a different owner later, grant default SELECT privileges as that table owner too.

## 2. Local test

Copy `.env.example` to `.env` and fill in the database connection values. Do not commit `.env`.

Build:

```bash
docker build -t azure-pg-mcp .
```

Run:

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
  azure-pg-mcp
```

MCP endpoint:

```text
http://localhost:8000/mcp
```

## 3. GitHub Container Registry

GitHub Actions builds and pushes these images automatically:

```text
ghcr.io/haowang2025/azure-pg-mcp:latest
ghcr.io/haowang2025/azure-pg-mcp:<commit-sha>
```

The workflow also starts the built container and verifies that port 8000 accepts connections.

Because this repository is private, the GHCR package may also be private. For the simplest Azure Container Apps deployment, change the package visibility to Public after the first successful build. Otherwise configure Azure Container Apps with GHCR credentials that have `read:packages` permission.

## 4. Azure Container Apps

Example low-cost Consumption configuration:

```bash
RG=pg-mcp
LOC=eastus
ENV=pg-mcp-env
APP=pg-mcp

az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App

az group create \
  --name "$RG" \
  --location "$LOC"

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

Store the database password as a Container Apps secret:

```bash
az containerapp secret set \
  --name "$APP" \
  --resource-group "$RG" \
  --secrets dbpassword='YOUR_MCP_DB_PASSWORD'
```

Configure environment variables:

```bash
az containerapp update \
  --name "$APP" \
  --resource-group "$RG" \
  --set-env-vars \
    DBHOST=YOUR_SERVER.postgres.database.azure.com \
    DBPORT=5432 \
    DBNAME=YOUR_DB \
    DBUSER=chatgpt_mcp \
    DBPASSWORD=secretref:dbpassword \
    SSLMODE=require \
    DB_CONNECT_TIMEOUT=10 \
    DB_STATEMENT_TIMEOUT_MS=15000 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp
```

Get the public hostname:

```bash
FQDN=$(az containerapp show \
  --name "$APP" \
  --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn \
  -o tsv)

echo "https://${FQDN}/mcp"
```

Use that HTTPS URL as the remote MCP endpoint in ChatGPT Developer Mode.

## 5. Azure PostgreSQL networking

For initial testing, the Container App must be able to reach PostgreSQL on port 5432. If the database uses public access, configure the Azure PostgreSQL firewall appropriately. For production/sensitive data, prefer private networking/VNet integration rather than broadly exposing the database.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `DBHOST` | yes | - | PostgreSQL hostname |
| `DBPORT` | no | `5432` | PostgreSQL port |
| `DBNAME` | yes | - | Database name |
| `DBUSER` | yes | - | Read-only role |
| `DBPASSWORD` | yes | - | Read-only role password |
| `SSLMODE` | no | `require` | PostgreSQL TLS mode |
| `DB_CONNECT_TIMEOUT` | no | `10` | Connect timeout in seconds |
| `DB_STATEMENT_TIMEOUT_MS` | no | `15000` | Query timeout in milliseconds |
| `MCP_HOST` | no | `0.0.0.0` | MCP bind address |
| `MCP_PORT` | no | `8000` | MCP port |
| `MCP_PATH` | no | `/mcp` | MCP Streamable HTTP path |

## Important

The current public MCP endpoint has no application-level authentication. The database role is deliberately read-only, but a public MCP URL can still expose readable data to anyone who can reach it. Use only non-sensitive data for the initial no-auth test, then add MCP authentication/private controls before using sensitive production data.
