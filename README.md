# LeanIX Workspace Snapshot Backup Script Documentation

## 1. Overview & Purpose

The **LeanIX Workspace Snapshot Backup** script is an enterprise-grade automation utility designed to back up full LeanIX workspaces programmatically. It triggers asynchronous full export jobs (Fact Sheet snapshots and optional archive snapshots), polls the LeanIX API for completion, downloads the resulting `.xlsx` archive in chunks, and logs the entire lifecycle for enterprise compliance and auditing.

### Key Capabilities
* **Asynchronous Export Management**: Requests and monitors background generation of `SNAPSHOT` and `ARCHIVE` export files.
* **Token Resilience & Auto-Refresh**: Seamlessly handles token expiration (`HTTP 401 Unauthorized`) mid-polling and re-authenticates without dropping the running job.
* **Streamed & Validated Downloads**: Streams files in 1 MB chunks to manage memory footprint, verifies file size, and inspects magic header bytes (`PK\x03\x04`) to validate valid zip/XLSX packaging.
* **Enterprise Security & Logging**: Incorporates token masking in log outputs and writes timestamped, rotating file logs alongside terminal output.
* **Audited Terminal UI**: Employs `rich` console styling constrained to bright green, bright gray, and bright orange terminal themes.

---

## 2. Architecture & Execution Flow

```text
+---------------------------------------------------------------+
|                        Initialization                         |
|   - Load environment variables via python-dotenv              |
|   - Configure rotating file logging (5 MB max, 5 backups)     |
|   - Validate local backup directories                         |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                    OAuth 2.0 Authentication                   |
|   - POST /services/mtm/v1/oauth2/token                        |
|   - Bearer token injected into persistent requests.Session    |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                     Export Request Phase                      |
|   - POST /services/pathfinder/v1/exports/fullExport           |
|   - Receives and logs assigned jobId                          |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                     Polling Loop & Retries                    |
|   - GET /services/pathfinder/v1/exports (pageSize=40, DESC)   |
|   - Checks for COMPLETED export created after run start       |
|   - Re-authenticates automatically if 401 occurs              |
|   - Timeout threshold: 4 hours (14,400 seconds)               |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                 Chunked Download & Verification               |
|   - GET /services/pathfinder/v1/exports/downloads/{WS_ID}     |
|   - Write to temporary `.part` file (1 MB buffer)             |
|   - Magic bytes check: Ensure first 2 bytes equal b"PK"       |
|   - Atomic rename: `.part` -> `.xlsx`                         |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                       Archive Execution                       |
|   - If LEANIX_INCLUDE_ARCHIVE=true, repeats for "ARCHIVE"     |
+---------------------------------------------------------------+
```

---

## 3. Environment Variables & Prerequisites

### 3.1 Python Dependencies
Install the required third-party libraries:

```bash
pip install requests python-dotenv rich
```

### 3.2 Environment Configuration (`.env`)
Create a `.env` file in the root execution directory (or export them in your operating system environment):

| Variable Name | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `PRD` | **Yes** | — | LeanIX technical user API token. |
| `SUBDOMAIN` | **Yes** | — | LeanIX tenant domain (e.g., `company.leanix.net`). |
| `WORKSPACE_ID.PRD` | **Yes** | — | UUID of the LeanIX workspace to download from. |
| `LEANIX_BACKUP_DIR` | No | `./leanix_backups` | Target folder for backup `.xlsx` files and logs. |
| `LEANIX_INCLUDE_ARCHIVE` | No | `false` | When set to `true`, also exports deleted/archived data. |

---

## 4. Module Breakdown & Key Functions

### 4.1 Token Masking (`mask_token`)
```python
mask_token(token: str) -> str
```
Prevents accidental leakage of credential strings into audit logs by reducing tokens to their first and last 4 characters (e.g., `abc1...9xyz`).

### 4.2 Authentication (`authenticate`)
```python
authenticate(session: requests.Session) -> str
```
Executes an OAuth2 `client_credentials` flow against `https://{SUBDOMAIN}/services/mtm/v1/oauth2/token` using HTTP Basic Authentication (`apitoken`, `API_TOKEN`). It updates `session.headers` directly with the returned Bearer token.

### 4.3 Triggering Export (`request_export`)
```python
request_export(session: requests.Session, export_type: str) -> str
```
Sends a `POST` request to `/services/pathfinder/v1/exports/fullExport` passing `exportType` (`SNAPSHOT` or `ARCHIVE`). Returns the assigned LeanIX `jobId`.

### 4.4 Status Polling & Detection
* **`get_exports(session, export_type)`**: Queries the recent export history (`/services/pathfinder/v1/exports`) sorted by creation date descending.
* **`find_completed_export(session, export_type, started_at)`**: Iterates through records, parses ISO timestamps to UTC, verifies that the completion occurred *after* script initiation, and extracts the ephemeral `downloadKey`.

### 4.5 Chunked Download & Validation (`download_export`)
```python
download_export(session: requests.Session, download_key: str, destination: Path) -> None
```
* Initiates a streamed `GET` request against `/services/pathfinder/v1/exports/downloads/{WORKSPACE_ID}` with header `Accept: application/octet-stream`.
* Streams content into a temporary staging file (`*.xlsx.part`) in 1 MB blocks while reporting real-time transfer progress.
* **Verification**: Checks that total bytes $> 0$ and confirms that the binary signature begins with `PK` (standard ZIP / OpenXML identifier for Excel files).
* Finalizes by atomically renaming `.part` to the target `.xlsx` path.

### 4.6 Orchestration Loop (`run_export`)
```python
run_export(session: requests.Session, export_type: str) -> Path
```
Monitors execution up to `TIMEOUT_SECONDS` (4 hours), re-authenticating on `401 Unauthorized` responses and polling every `POLL_SECONDS` (10 seconds).

---

## 5. UI & Console Theme Specifications

The terminal interface uses the Python `rich` library with a restricted color palette:

| Element | Visual Treatment | Technical Spec |
| :--- | :--- | :--- |
| **Progress / Highlights / Warnings** | Bright Orange | Hex `#FF8800` |
| **Success / Completed Operations** | Bright Green | Terminal `bright_green` |
| **Metadata / File Names / Secondary Info** | Bright Gray | Terminal `bright_black` |

---

## 6. Error Handling & Recovery Matrix

| Failure Mode | Root Cause | Automated Recovery / Behavior |
| :--- | :--- | :--- |
| **Missing API Key** | `PRD` variable is empty | Raises `ValueError`, logs critical alert, halts execution. |
| **Mid-run Expiration** | Token expires during long workspace export | Polling loop detects `HTTP 401`, triggers `authenticate()`, and resumes polling without failing the job. |
| **Incomplete Download** | Network drop during file transfer | Progress context raises exception, cleans up incomplete `.part` file, and exits with non-zero exit code. |
| **Corrupted Artifact** | API served error JSON instead of binary | Magic bytes check fails (header $\neq$ `b"PK"`), staging file is deleted, script raises `RuntimeError`. |
| **Process Interruption** | Manual `SIGINT` (Ctrl+C) | Trapped by `KeyboardInterrupt`, writes warning to log, outputs bright orange alert, exits with code `130`. |

---

## 7. Production Automation (Cron Example)

To automate this script as a daily cron job on Linux or macOS:

```bash
# Open crontab
crontab -e

# Run every morning at 02:00 UTC
0 2 * * * cd /opt/scripts/leanix-backup && /usr/bin/python3 backup_script.py >> /var/log/leanix_cron.log 2>&1
```
