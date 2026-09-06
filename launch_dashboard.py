"""GovAward Alpha — Startup & Background Dashboard Launcher.

Starts the Uvicorn server in the background and opens the default browser
to http://127.0.0.1:8050/ as soon as the service is ready.
If the dashboard is already running, it brings up the browser tab and exits.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Ensure project root and src directory are in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

os.chdir(PROJECT_ROOT)

URL = "http://127.0.0.1:8050/"


def is_server_ready() -> bool:
    """Check if the dashboard server is responding."""
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "GovAwardLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def pop_browser() -> None:
    """Wait for server to be responsive, then open default browser."""
    # Poll for up to 15 seconds (30 attempts x 0.5s)
    for _ in range(30):
        if is_server_ready():
            webbrowser.open(URL)
            return
        time.sleep(0.5)
    # If timeout reached, try opening anyway
    webbrowser.open(URL)


def main() -> None:
    # 1. If server is already running, simply focus/open the browser and exit cleanly
    if is_server_ready():
        webbrowser.open(URL)
        sys.exit(0)

    # 2. Spawn a background thread to pop open the default browser once server is alive
    browser_thread = threading.Thread(target=pop_browser, daemon=True)
    browser_thread.start()

    # 3. Ensure database is initialized / migrated
    try:
        from govaward_alpha.db import init_db
        init_db()
    except Exception as exc:
        pass

    # 4. Run Uvicorn server
    import uvicorn
    uvicorn.run(
        "govaward_alpha.dashboard.app:app",
        host="127.0.0.1",
        port=8050,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
