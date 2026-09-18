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

URL = "http://127.0.0.1:8055/"


def is_server_ready() -> bool:
    """Check if the dashboard server or tunnel is responding."""
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "GovAwardLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> None:
    # 1. Start the persistent background tunnel watchdog if not already running
    import subprocess
    vbs_path = PROJECT_ROOT / "start_tunnel_hidden.vbs"
    if vbs_path.exists():
        subprocess.run(["wscript.exe", str(vbs_path)], check=False)

    # 2. Wait up to 10 seconds for the tunnel to respond
    for _ in range(20):
        if is_server_ready():
            break
        time.sleep(0.5)

    # 3. Open the dashboard in default browser
    webbrowser.open(URL)
    sys.exit(0)


if __name__ == "__main__":
    main()
