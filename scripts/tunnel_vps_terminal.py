"""
Resilient Local SSH Tunnel Daemon for GovAward Alpha Terminal.
=============================================================
Provides a self-healing, immortal local tunnel forwarding 127.0.0.1:8055
to the 24/7 VPS GovAward Alpha Financial Terminal.

Key Features:
- Supervised process watchdog (auto-restarts on network drop, WiFi switch, or sleep/wake).
- Prioritizes high-throughput native OpenSSH (ssh.exe) with aggressive keep-alive.
- Falls back to Paramiko Python transport if native OpenSSH is unavailable.
- Active HTTP health probe (recycles frozen/zombie connections within 30-45s).
- Pythonw-safe logging to logs/tunnel.log (rotating, max 5MB).
- Single-instance lock to prevent port collisions.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import signal
import subprocess
import threading
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
PID_FILE = DATA_DIR / "tunnel.pid"
LOG_FILE = LOGS_DIR / "tunnel.log"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Safe stream redirection for Windows pythonw (GUI subsystem)
_is_gui = False
try:
    if sys.stdout is None or not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        _is_gui = True
except Exception:
    _is_gui = True

if _is_gui:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(str(LOGS_DIR / "tunnel_stderr.log"), "a", encoding="utf-8")
    except Exception:
        pass

# Ultra-reliable zero-dependency direct rotating logger
def _append_log(msg: str, level: str = "INFO") -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}\n"
    try:
        # Check size for log rotation (max 5MB)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            backup = LOGS_DIR / "tunnel.log.1"
            if backup.exists():
                backup.unlink(missing_ok=True)
            LOG_FILE.rename(backup)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    if not _is_gui and sys.stdout:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass

class _LoggerWrapper:
    def info(self, msg: str): _append_log(msg, "INFO")
    def warning(self, msg: str): _append_log(msg, "WARNING")
    def error(self, msg: str, exc_info: bool = False): _append_log(msg, "ERROR")
    def critical(self, msg: str, exc_info: bool = False): _append_log(msg, "CRITICAL")
    def debug(self, msg: str): pass

logger = _LoggerWrapper()

# Load credentials from .env
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
    except Exception as exc:
        logger.warning(f"Could not read .env: {exc}")

LOCAL_PORT = 8055
REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = 8055
SSH_HOST = os.environ.get("VPS_HOST", "vmi3134862.contaboserver.net")
SSH_USER = os.environ.get("VPS_USER", "root")
SSH_PASS = os.environ.get("VPS_PASSWORD") or os.environ.get("VPS_PASS", "")



def is_port_in_use(port: int) -> bool:
    """Check if local TCP port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def probe_tunnel_health(port: int, timeout: float = 3.0) -> bool:
    """Perform a quick HTTP ping through the tunnel."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return False
            s.sendall(f"GET /api/stats HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("utf-8"))
            data = s.recv(256)
            return b"200 OK" in data or b"total_alerts" in data or b"HTTP/1." in data
    except Exception:
        return False


# Named Mutex to guarantee single instance without race conditions
_MUTEX_HANDLE = None

def acquire_single_instance_lock(name: str = "GovAwardAlpha_Tunnel_Daemon_Mutex") -> bool:
    """Ensure exactly one tunnel daemon runs system-wide using Windows kernel mutex."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CreateMutexW = kernel32.CreateMutexW
        CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE
        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        ERROR_ALREADY_EXISTS = 183
        handle = CreateMutexW(None, False, name)
        if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            if handle:
                CloseHandle(handle)
            # If another daemon holds the mutex but port probe fails (e.g. sleep/wake freeze),
            # kill the frozen SSH socket to force the supervisor to restart immediately.
            if not probe_tunnel_health(LOCAL_PORT, timeout=2.0):
                logger.warning("Existing daemon mutex held, but port 8055 is unresponsive. Force-recycling stale listener...")
                kill_orphan_port_listener(LOCAL_PORT)
                time.sleep(1.5)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        logger.warning(f"Could not initialize Windows mutex: {exc}")
        return True


def kill_orphan_port_listener(port: int = 8055) -> None:
    """Terminate any orphan ssh/python process listening on local port."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.name() or "").lower()
                if "ssh" in name or "python" in name:
                    conn_fn = getattr(proc, "net_connections", None) or getattr(proc, "connections", None)
                    if conn_fn:
                        for conn in conn_fn(kind="tcp"):
                            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
                                if proc.pid != os.getpid():
                                    logger.info(f"Terminating orphan {proc.name()} (PID {proc.pid}) listening on port {port}...")
                                    proc.terminate()
                                    try:
                                        proc.wait(timeout=2)
                                    except Exception:
                                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as exc:
        logger.debug(f"Could not check orphan port listener: {exc}")


def write_pid_file():
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Could not write PID file: {exc}")


def remove_pid_file():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Native OpenSSH Tunnel Worker
# ---------------------------------------------------------------------------

class NativeSshWorker:
    """Manages an OpenSSH (ssh.exe) subprocess with active health recycling."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._consecutive_probe_failures = 0
        self._is_online = False

    def start(self) -> subprocess.Popen:
        cmd = [
            "ssh.exe",
            "-N",
            "-T",
            "-L", f"{LOCAL_PORT}:{REMOTE_HOST}:{REMOTE_PORT}",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "TCPKeepAlive=yes",
            "-o", "ConnectTimeout=8",
            f"{SSH_USER}@{SSH_HOST}",
        ]
        CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
        logger.info(f"Spawning native OpenSSH tunnel: {SSH_USER}@{SSH_HOST} -> {LOCAL_PORT}")
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return self.process

    def stop(self):
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    def health_monitor_loop(self):
        """Monitor tunnel responsiveness; kill process if frozen."""
        time.sleep(2)  # Allow initial handshake
        while not self._stop_event.is_set():
            if self.process and self.process.poll() is None:
                if probe_tunnel_health(LOCAL_PORT, timeout=2.5):
                    if not self._is_online:
                        self._is_online = True
                        logger.info(f"[TUNNEL ONLINE] http://127.0.0.1:{LOCAL_PORT} -> VPS {REMOTE_HOST}:{REMOTE_PORT}")
                    self._consecutive_probe_failures = 0
                else:
                    self._consecutive_probe_failures += 1
                    logger.warning(f"Tunnel health probe failed ({self._consecutive_probe_failures}/2)")
                    if self._consecutive_probe_failures >= 2:
                        logger.error("Tunnel unresponsive for ~10s. Recycling ssh process...")
                        self._is_online = False
                        self._consecutive_probe_failures = 0
                        if self.process:
                            try:
                                self.process.terminate()
                                self.process.wait(timeout=2)
                            except Exception:
                                try:
                                    self.process.kill()
                                except Exception:
                                    pass
            time.sleep(5 if not self._is_online else 10)


# ---------------------------------------------------------------------------
# Paramiko Fallback Worker
# ---------------------------------------------------------------------------

def _forward_sockets(source: socket.socket, destination: socket.socket):
    try:
        while True:
            data = source.recv(32768)
            if not data:
                break
            destination.sendall(data)
    except Exception:
        pass
    finally:
        for s in (destination, source):
            try:
                s.close()
            except Exception:
                pass


class ParamikoFallbackTunnel:
    """Pure-Python Paramiko SSH tunnel fallback."""

    def __init__(self):
        self.ssh = None
        self.transport = None
        self.lock = threading.Lock()
        self.running = True

    def connect(self) -> bool:
        import paramiko
        with self.lock:
            if self.transport and self.transport.is_active():
                return True
            if self.ssh:
                try:
                    self.ssh.close()
                except Exception:
                    pass
            logger.info(f"[Paramiko] Connecting tunnel to {SSH_HOST}...")
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=12)
            self.transport = self.ssh.get_transport()
            if self.transport:
                self.transport.set_keepalive(30)
                logger.info("[Paramiko] SSH Transport connected and 30s keep-alive enabled.")
                return True
            return False

    def is_alive(self) -> bool:
        return self.transport is not None and self.transport.is_active()

    def handle_client(self, client_socket: socket.socket):
        if not self.is_alive():
            try:
                if not self.connect():
                    client_socket.close()
                    return
            except Exception as e:
                logger.warning(f"[Paramiko] Reconnect failed: {e}")
                client_socket.close()
                return

        try:
            remote_channel = self.transport.open_channel(  # type: ignore[union-attr]
                "direct-tcpip",
                (REMOTE_HOST, REMOTE_PORT),
                client_socket.getpeername(),
            )
        except Exception as e:
            logger.warning(f"[Paramiko] open_channel error ({e}), retrying once...")
            try:
                self.connect()
                remote_channel = self.transport.open_channel(  # type: ignore[union-attr]
                    "direct-tcpip",
                    (REMOTE_HOST, REMOTE_PORT),
                    client_socket.getpeername(),
                )
            except Exception as e2:
                logger.warning(f"[Paramiko] Reconnect retry failed: {e2}")
                client_socket.close()
                return

        if remote_channel is None:
            client_socket.close()
            return

        t1 = threading.Thread(target=_forward_sockets, args=(client_socket, remote_channel), daemon=True)
        t2 = threading.Thread(target=_forward_sockets, args=(remote_channel, client_socket), daemon=True)
        t1.start()
        t2.start()

    def run(self):
        while self.running:
            try:
                if self.connect():
                    break
            except Exception as e:
                logger.warning(f"[Paramiko] Initial connection failed: {e}. Retrying in 5s...")
                time.sleep(5)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SOL_REUSEADDR, 1)
        server.bind(("127.0.0.1", LOCAL_PORT))
        server.listen(32)
        logger.info(f"[Paramiko] Listening on http://127.0.0.1:{LOCAL_PORT} -> VPS :{REMOTE_PORT}")

        while self.running:
            try:
                client, _ = server.accept()
                threading.Thread(target=self.handle_client, args=(client,), daemon=True).start()
            except Exception as e:
                logger.warning(f"[Paramiko] Accept error: {e}")
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Immortal Watchdog Daemon
# ---------------------------------------------------------------------------

class TunnelWatchdog:
    """Supervisory watchdog loop ensuring the tunnel runs 24/7 forever."""

    def __init__(self):
        self.running = True
        self._current_worker: NativeSshWorker | None = None
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}. Terminating watchdog gracefully.")
        self.running = False
        if self._current_worker:
            try:
                self._current_worker.stop()
            except Exception:
                pass

    def run(self):
        if not acquire_single_instance_lock():
            logger.info("Tunnel daemon already running (locked by another instance). Exiting redundant process.")
            return

        write_pid_file()
        kill_orphan_port_listener(LOCAL_PORT)
        logger.info(f"============================================================")
        logger.info(f"GovAward Alpha Resilient Tunnel Watchdog Started (PID {os.getpid()})")
        logger.info(f"Target: {SSH_USER}@{SSH_HOST} | Forward: 127.0.0.1:{LOCAL_PORT} -> :{REMOTE_PORT}")
        logger.info(f"============================================================")

        # Check if native OpenSSH is present
        has_ssh_cli = False
        CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
        try:
            res = subprocess.run(
                ["ssh.exe", "-V"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            has_ssh_cli = res.returncode == 0 or "openssh" in (res.stderr or "").lower()
        except Exception as exc:
            logger.warning(f"Failed to query OpenSSH version: {exc}")
            has_ssh_cli = False

        if has_ssh_cli:
            logger.info("Native OpenSSH detected. Using high-performance OpenSSH tunnel worker.")
            self._run_native_watchdog()
        else:
            logger.info("Native OpenSSH not available. Falling back to Paramiko Python tunnel.")
            try:
                ParamikoFallbackTunnel().run()
            finally:
                remove_pid_file()

    def _run_native_watchdog(self):
        backoff = 2
        while self.running:
            try:
                # If local port is already occupied (e.g. leftover zombie), clear it
                if is_port_in_use(LOCAL_PORT) and not probe_tunnel_health(LOCAL_PORT):
                    logger.warning(f"Port {LOCAL_PORT} occupied by dead socket. Terminating zombie occupant...")
                    kill_orphan_port_listener(LOCAL_PORT)
                    time.sleep(1)

                worker = NativeSshWorker()
                self._current_worker = worker
                proc = worker.start()

                # Start background health probe
                probe_thread = threading.Thread(target=worker.health_monitor_loop, daemon=True)
                probe_thread.start()

                # Wait for initial binding
                time.sleep(2)
                if probe_tunnel_health(LOCAL_PORT, timeout=3.0):
                    logger.info(f"[TUNNEL ONLINE] http://127.0.0.1:{LOCAL_PORT} -> VPS {REMOTE_HOST}:{REMOTE_PORT}")
                    backoff = 2  # Reset backoff on successful connection

                # Supervise process until exit
                exit_code = proc.wait()
                worker.stop()
                logger.warning(f"SSH process exited with code {exit_code}.")

            except Exception as exc:
                logger.error(f"Watchdog exception: {exc}", exc_info=True)

            if self.running:
                logger.info(f"Reconnecting tunnel in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30)

        remove_pid_file()
        logger.info("Tunnel watchdog stopped.")


if __name__ == "__main__":
    try:
        TunnelWatchdog().run()
    except Exception as exc:
        logger.critical(f"Unhandled crash in TunnelWatchdog: {exc}", exc_info=True)
        remove_pid_file()
        sys.exit(1)
