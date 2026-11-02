"""
LeanIX Workspace Snapshot Backup Script (With Extensive Logging)

This script automates the process of creating and downloading a full workspace 
backup (snapshot) from LeanIX. 

Features:
- Beautiful terminal UI using `rich`.
- Extensive background logging to a file for enterprise auditing/troubleshooting.
- Automatic API token masking in logs.
- Resilient polling and chunked downloads.
"""

import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)
from rich.panel import Panel


# ==========================================
# 1. Configuration & Constants
# ==========================================
API_TOKEN       = os.getenv('PRD')
SUBDOMAIN       = os.getenv('SUBDOMAIN')
WORKSPACE_ID    = os.getenv('WORKSPACE_ID.PRD')

BACKUP_DIR = Path(os.environ.get("LEANIX_BACKUP_DIR", "./leanix_backups"))
LOG_DIR = BACKUP_DIR

INCLUDE_ARCHIVE = os.environ.get("LEANIX_INCLUDE_ARCHIVE", "false").lower() == "true"

POLL_SECONDS = 10                  
TIMEOUT_SECONDS = 4 * 60 * 60      

BASE_URL = f"https://{SUBDOMAIN}"
API_ROOT = f"{BASE_URL}/services/pathfinder/v1"
AUTH_URL = f"{BASE_URL}/services/mtm/v1/oauth2/token"

console = Console()

# Defined strictly within the requested palette
COLOR_GREEN  = "bright_green"
COLOR_GRAY   = "bright_black"
COLOR_ORANGE = "#FF8800"

# ==========================================
# 2. Extensive Logging Setup
# ==========================================
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / "leanix_backup.log"

# Set up a rotating file handler (keeps the last 5 logs, max 5MB each)
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(funcName)-20s | %(message)s"
))

logger = logging.getLogger("LeanIX_Backup")
logger.setLevel(logging.DEBUG) # DEBUG captures EVERYTHING. Change to INFO for less noise.
logger.addHandler(file_handler)

def mask_token(token):
    """Utility to mask sensitive tokens in the logs."""
    if not token or len(token) < 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


# ==========================================
# 3. Authentication Logic
# ==========================================
def authenticate(session):
    logger.info("Starting authentication process.")
    logger.debug(f"Using Auth URL: {AUTH_URL}")
    logger.debug(f"API Token ends in: {mask_token(API_TOKEN)}")

    if not API_TOKEN:
        logger.critical("API_TOKEN is None. Please ensure 'ArchiMEDES.PRD' environment variable is set.")
        raise ValueError("API_TOKEN environment variable 'ArchiMEDES.PRD' is not set.")

    with console.status(f"[bold {COLOR_ORANGE}]Authenticating with LeanIX...", spinner="dots", spinner_style=COLOR_ORANGE):
        response = session.post(
            AUTH_URL,
            auth=("apitoken", API_TOKEN), 
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        
        if response.status_code != 200:
            logger.error(f"Authentication failed with status {response.status_code}. Response: {response.text}")
        response.raise_for_status() 

        access_token = response.json().get("access_token")
        if not access_token:
            logger.critical("Authentication response was successful but contained no access_token.")
            raise RuntimeError("Authentication response did not contain access_token.")

        session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })
        
    logger.info(f"Successfully authenticated. Session Bearer token updated (Masked: {mask_token(access_token)}).")
    console.print(f"[{COLOR_GREEN}]✔ Authenticated successfully![/{COLOR_GREEN}]")
    return access_token


# ==========================================
# 4. Requesting the Export
# ==========================================
def request_export(session, export_type):
    url = f"{API_ROOT}/exports/fullExport"
    logger.info(f"Requesting new {export_type} export job.")
    logger.debug(f"POST {url} | Payload: exportType={export_type}")

    with console.status(f"[bold {COLOR_ORANGE}]Initiating {export_type} export...", spinner="dots", spinner_style=COLOR_ORANGE):
        response = session.post(
            url,
            params={"exportType": export_type},
            timeout=60,
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to initiate {export_type} export. Status: {response.status_code} | Body: {response.text}")
        response.raise_for_status()

        result = response.json()
        if result.get("status") != "OK":
            logger.error(f"API rejected the export request: {result}")
            raise RuntimeError(
                f"LeanIX did not accept the {export_type} export: "
                f"{json.dumps(result)[:2000]}"
            )

        job_id = result.get("data", {}).get("jobId")
        logger.info(f"Export job successfully queued. Assigned Job ID: {job_id}")
        
    console.print(f"[{COLOR_GREEN}]✔ Started {export_type} export![/{COLOR_GREEN}] [{COLOR_GRAY}]Job ID: {job_id}[/{COLOR_GRAY}]")
    return job_id


# ==========================================
# 5. Polling & Status Checking
# ==========================================
def get_exports(session, export_type):
    logger.debug(f"Fetching recent exports of type: {export_type} from API.")
    response = session.get(
        f"{API_ROOT}/exports",
        params={
            "exportType": export_type,
            "pageSize": 40,
            "sorting": "createdAt",
            "sortDirection": "DESC",
        },
        timeout=60,
    )
    
    if response.status_code != 200:
        logger.warning(f"Failed to fetch recent exports. Status: {response.status_code}")
    response.raise_for_status()

    body = response.json()
    records = body.get("data", [])
    logger.debug(f"Retrieved {len(records)} historical export records.")

    if not isinstance(records, list):
        logger.critical(f"Unexpected API response format: {str(body)[:500]}")
        raise RuntimeError("Unexpected export-list response: " + json.dumps(body)[:2000])

    return records


def find_completed_export(session, export_type, started_at):
    logger.debug(f"Scanning for completed {export_type} exports created after {started_at.isoformat()}")
    records = get_exports(session, export_type)

    for record in records:
        if not isinstance(record, dict):
            continue

        status = str(record.get("status", "")).upper()
        
        if status != "COMPLETED":
            continue 

        timestamp = (
            record.get("createdAt")
            or record.get("created_at")
            or record.get("creationDate")
        )

        if timestamp:
            try:
                created = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                    
                if created < started_at:
                    logger.debug(f"Skipping old export ({status}) created at {created.isoformat()}")
                    continue 
            except ValueError as e:
                logger.warning(f"Failed to parse timestamp '{timestamp}': {e}")
                continue
        else:
            logger.warning(f"Record found without a valid timestamp. Skipping.")
            continue 

        download_key = record.get("downloadKey") or record.get("download_key")

        if download_key:
            logger.info(f"Target export found! Status: {status} | Download Key: {mask_token(download_key)}")
            return download_key

    logger.debug(f"Target export not yet complete. Continuing to poll...")
    return None


# ==========================================
# 6. File Downloading
# ==========================================
def download_export(session, download_key, destination):
    url = f"{API_ROOT}/exports/downloads/{WORKSPACE_ID}"
    logger.info(f"Initiating download. Target file: {destination}")
    logger.debug(f"GET {url} | Key: {mask_token(download_key)}")

    response = session.get(
        url,
        params={"key": download_key},
        headers={"Accept": "application/octet-stream"},
        stream=True,
        timeout=(30, 300),
    )
    
    if response.status_code != 200:
        logger.error(f"Download failed. Status: {response.status_code} | Reason: {response.reason}")
    response.raise_for_status()

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")
    
    total_size = int(response.headers.get("content-length", 0)) or None
    logger.debug(f"Reported file size: {total_size} bytes")

    try:
        with Progress(
            SpinnerColumn(style=COLOR_ORANGE),
            TextColumn(f"[{COLOR_ORANGE}]{{task.description}}"),
            BarColumn(complete_style=COLOR_GREEN, finished_style=COLOR_GREEN),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Downloading {destination.name}...", total=total_size)
            
            with temp_path.open("wb") as output:
                chunk_count = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024): 
                    if chunk:
                        output.write(chunk)
                        progress.update(task, advance=len(chunk))
                        chunk_count += 1
                logger.debug(f"Finished writing {chunk_count} chunks to temporary file.")

        if temp_path.stat().st_size == 0:
            logger.error("Downloaded file is exactly 0 bytes.")
            raise RuntimeError("Downloaded export is empty.")

        with temp_path.open("rb") as file:
            magic_bytes = file.read(2)
            if magic_bytes != b"PK":
                logger.error(f"Invalid file signature detected: {magic_bytes}. Expected b'PK'.")
                raise RuntimeError(
                    "Download does not look like an XLSX file. "
                    "Inspect API permissions and the response format."
                )

        temp_path.replace(destination)
        logger.info(f"Temporary file renamed. Download successfully completed: {destination}")
        
    finally:
        if temp_path.exists():
            logger.warning(f"Cleaning up stranded temporary file: {temp_path}")
            temp_path.unlink()

    file_size_mb = destination.stat().st_size / (1024 * 1024)
    console.print(f"[bold {COLOR_GREEN}]✔ Download complete:[/{COLOR_GREEN}] [{COLOR_GRAY}]{destination.name} ({file_size_mb:.2f} MB)[/{COLOR_GRAY}]")


# ==========================================
# 7. Primary Orchestration Loop
# ==========================================
def run_export(session, export_type):
    started_at = datetime.now(timezone.utc)
    logger.info(f"--- Starting new export lifecycle for: {export_type} ---")
    
    request_export(session, export_type)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    logger.debug(f"Polling deadline set for {TIMEOUT_SECONDS} seconds from now.")

    with console.status(f"[bold {COLOR_ORANGE}]Waiting for {export_type} to generate on LeanIX servers...", spinner="point", spinner_style=COLOR_ORANGE) as status:
        
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS) 

            try:
                download_key = find_completed_export(session, export_type, started_at)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    logger.warning("OAuth Token expired during polling loop. Re-authenticating.")
                    status.update(f"[bold {COLOR_ORANGE}]Token expired, re-authenticating...[/bold {COLOR_ORANGE}]")
                    authenticate(session)
                    status.update(f"[bold {COLOR_ORANGE}]Waiting for {export_type} to generate on LeanIX servers...")
                    continue
                
                logger.error(f"HTTPError encountered during polling: {exc}")
                raise

            if download_key:
                console.print(f"[{COLOR_GREEN}]✔ {export_type} snapshot is ready for download![/{COLOR_GREEN}]")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"leanix_{export_type.lower()}_{timestamp}.xlsx"
                destination = BACKUP_DIR / filename
                
                download_export(session, download_key, destination)
                logger.info(f"Lifecycle for {export_type} completed successfully.")
                return destination

    logger.critical(f"Polling for {export_type} timed out after {TIMEOUT_SECONDS} seconds.")
    raise TimeoutError(
        f"{export_type} export was not downloadable within "
        f"{TIMEOUT_SECONDS // 3600} hours."
    )


# ==========================================
# 8. Main Execution Entry Point
# ==========================================
def main():
    logger.info("========== SCRIPT INITIATED ==========")
    console.print(Panel.fit(f"[bold {COLOR_ORANGE}]LeanIX Workspace Snapshot Backup[/bold {COLOR_ORANGE}]", border_style=COLOR_ORANGE))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Ensuring backup directory exists: {BACKUP_DIR.resolve()}")

    with requests.Session() as session:
        authenticate(session)

        snapshot = run_export(session, "SNAPSHOT")
        
        if INCLUDE_ARCHIVE:
            logger.info("INCLUDE_ARCHIVE flag is True. Starting archive export.")
            archive = run_export(session, "ARCHIVE")
        else:
            logger.info("INCLUDE_ARCHIVE flag is False. Skipping archive export.")
            
    logger.info("========== SCRIPT COMPLETED SUCCESSFULLY ==========")
    console.print(Panel.fit(
        f"[bold {COLOR_GREEN}]Backup process completed successfully.[/bold {COLOR_GREEN}]\n"
        f"[{COLOR_GRAY}]Files saved in:[/{COLOR_GRAY}] [{COLOR_ORANGE}]{BACKUP_DIR.resolve()}[/{COLOR_ORANGE}]\n"
        f"[{COLOR_GRAY}]Logs saved in:[/{COLOR_GRAY}] [{COLOR_ORANGE}]{log_file.resolve()}[/{COLOR_ORANGE}]", 
        title=f"[bold {COLOR_GREEN}]Success[/bold {COLOR_GREEN}]", 
        border_style=COLOR_GREEN
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Script execution interrupted manually by user (KeyboardInterrupt).")
        console.print(f"\n[bold {COLOR_ORANGE}]✖ Interrupted by user.[/bold {COLOR_ORANGE}]")
        sys.exit(130)
    except Exception as e:
        logger.exception("An unhandled exception caused the script to fail.")
        error_msg = f"An API error occurred: {str(e)}"
        if isinstance(e, requests.HTTPError) and e.response is not None:
            error_msg += f"\n\n[bold {COLOR_ORANGE}]Response content:[/bold {COLOR_ORANGE}]\n{e.response.text}"
        console.print(Panel(error_msg, title=f"[bold {COLOR_ORANGE}]Backup Failed[/bold {COLOR_ORANGE}]", border_style=COLOR_ORANGE, style=COLOR_ORANGE))
        sys.exit(1)
