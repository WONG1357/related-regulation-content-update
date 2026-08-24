from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


APP_TITLE = "Regulatory Impact Review"
STARTUP_TIMEOUT_SECONDS = 45


def resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def runtime_root() -> Path:
    override = os.getenv("REGULATORY_DESKTOP_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "Regulatory Impact Review"


def documents_root() -> Path:
    override = os.getenv("REGULATORY_DESKTOP_DOCUMENTS", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "Regulatory Impact Review"


def prepare_runtime_environment() -> dict[str, Path]:
    state = runtime_root()
    config = state / "config"
    data = state / "data"
    logs = state / "logs"
    original_files = documents_root() / "original file"
    for directory in (config, data, logs, original_files):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ["REGULATORY_APP_CONFIG_DIR"] = str(config)
    os.environ["REGULATORY_APP_DATA_DIR"] = str(data)
    os.environ["REGULATORY_ORIGINAL_FILE_DIR"] = str(original_files)
    return {
        "state": state,
        "config": config,
        "data": data,
        "logs": logs,
        "original_files": original_files,
    }


def choose_port(preferred: int = 8501) -> int:
    candidates = (0,) if preferred == 0 else (preferred, 0)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(candidate.getsockname()[1])
    raise RuntimeError("No local port is available.")


def child_command(port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--streamlit-child", str(port)]
    return [sys.executable, str(Path(__file__).resolve()), "--streamlit-child", str(port)]


def run_streamlit_child(port: int) -> None:
    root = resource_root()
    os.chdir(root)
    sys.path.insert(0, str(root))
    from streamlit.web import bootstrap

    flag_options = {
        "global.developmentMode": False,
        "server.address": "127.0.0.1",
        "server.port": port,
        "server.headless": True,
        "server.fileWatcherType": "none",
        "browser.gatherUsageStats": False,
    }
    bootstrap.load_config_options(flag_options)
    bootstrap.run(
        str(root / "app.py"),
        False,
        [],
        flag_options,
    )


def wait_for_health(port: int, process: subprocess.Popen[bytes]) -> None:
    health_url = f"http://127.0.0.1:{port}/_stcore/health"
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"The local server stopped with exit code {process.returncode}.")
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("The local server did not become ready in time.")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def start_server() -> tuple[subprocess.Popen[bytes], int, object]:
    paths = prepare_runtime_environment()
    port = choose_port()
    log_handle = (paths["logs"] / "desktop-server.log").open("ab")
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        child_command(port),
        cwd=resource_root(),
        env=os.environ.copy(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    try:
        wait_for_health(port, process)
    except Exception:
        stop_process(process)
        log_handle.close()
        raise
    return process, port, log_handle


def show_error(message: str) -> None:
    import webview

    escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    webview.create_window(
        APP_TITLE,
        html=f"<main style='font:16px sans-serif;padding:32px'><h2>Unable to start</h2><p>{escaped}</p></main>",
        width=620,
        height=320,
    )
    webview.start()


def run_desktop() -> int:
    try:
        process, port, log_handle = start_server()
    except Exception as exc:
        show_error(str(exc))
        return 1
    try:
        import webview

        webview.create_window(
            APP_TITLE,
            f"http://127.0.0.1:{port}",
            width=1440,
            height=920,
            min_size=(1024, 700),
            text_select=True,
        )
        webview.start(debug=False)
        return 0
    finally:
        stop_process(process)
        log_handle.close()


def run_smoke_test() -> int:
    process, _port, log_handle = start_server()
    stop_process(process)
    log_handle.close()
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--streamlit-child":
        run_streamlit_child(int(sys.argv[2]))
        return 0
    if sys.argv[1:] == ["--smoke-test"]:
        return run_smoke_test()
    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
