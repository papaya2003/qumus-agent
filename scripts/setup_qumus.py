"""Prepare local environments, private config and the single desktop launcher.

Run with Python 3.11 or newer. --local-only writes local support files without
installing packages. Existing .env, run data and editor preferences are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
HIDDEN = ("**/.git", "**/.venv", "**/.venv-v1compare", "**/__pycache__",
          "**/.pytest_cache", "**/.ruff_cache", "**/.mypy_cache", "**/*.egg-info")


def launcher_names() -> tuple[str, str]:
    """Windows: double-click .bat launchers; macOS: .command (both run scripts/start_qumus.py)."""
    return (("Start Qumus.bat", "Stop Qumus.bat") if WINDOWS
            else ("Start Qumus.command", "Stop Qumus.command"))


def venv_python(env: Path) -> Path:
    """The interpreter inside a virtual environment on this system."""
    return env / ("Scripts/python.exe" if WINDOWS else "bin/python")


def install_environment(folder: str, extras: str) -> None:
    env = REPO / folder
    python = venv_python(env)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    subprocess.run([str(python), "-m", "pip", "install", "-e", f".[{extras}]"],
                   cwd=REPO, check=True)


def write_editor_settings(root: Path) -> None:
    path = root / ".vscode" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(settings, dict) or not isinstance(settings.get("files.exclude", {}), dict):
            raise ValueError("settings must contain objects")
    except (ValueError, OSError):
        print(f"Kept custom editor settings unchanged: {path}")
        return
    exclude = settings.setdefault("files.exclude", {})
    for pattern in HIDDEN:
        exclude.setdefault(pattern, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _read_exact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:   # Path.read_text(newline=) is 3.13+
        return handle.read()


def prepare_local_files(*, refresh_launcher: bool = False) -> None:
    config = REPO / ".env"
    if not config.exists():
        with config.open("x", encoding="utf-8") as handle:
            config.chmod(0o600)
            handle.write((REPO / ".env.example").read_text(encoding="utf-8"))
    for name in ("checks", "archive"):
        (REPO / "runs" / name).mkdir(parents=True, exist_ok=True)
    # The launcher creates its workspace only after checking for unmigrated data.
    root = REPO.parent
    for name in launcher_names():
        launcher = root / name
        # newline="": keep the template's line endings (.bat needs CRLF)
        source = _read_exact(REPO / "scripts" / "templates" / name)
        # .bat: relative to the launcher's own folder (%~dp0 = REPO.parent\). cmd.exe
        # reads batch files in the OEM code page (GBK on a Chinese Windows), so an
        # absolute path with non-ASCII folder names would be garbled.
        repo = "%~dp0" + REPO.name if name.endswith(".bat") else shlex.quote(str(REPO))
        source = source.replace("@QUMUS_REPO@", repo)
        if (not launcher.exists() or refresh_launcher
                or _read_exact(launcher) == source):
            launcher.write_text(source, encoding="utf-8", newline="")
            launcher.chmod(0o755)
        else:
            print(f"Kept existing {name}. Use --refresh-launcher to regenerate it from this repository.")
        print(f"Launcher: {launcher}")
    for folder in (root, REPO):
        write_editor_settings(folder)
    print("Edit private .env values before sending a model task. Setup makes no model calls.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true", help="skip environment/package installation")
    parser.add_argument("--with-v1", action="store_true", help="also install the separate frozen-v1 environment")
    parser.add_argument("--ui-tests", action="store_true", help="install Playwright and Chromium for browser tests")
    parser.add_argument("--refresh-launcher", action="store_true", help="replace the root launcher with the tracked template")
    args = parser.parse_args()
    if sys.version_info < (3, 11):  # noqa: UP036 - bootstrap runs before package installation
        parser.error("Python 3.11 or newer is required")
    if args.local_only and (args.with_v1 or args.ui_tests):
        parser.error("--local-only cannot be combined with package installation options")
    if not args.local_only:
        extras = "dev,agents,vision,design" + (",ui-test" if args.ui_tests else "")
        install_environment(".venv", extras)
        if args.with_v1:
            install_environment(".venv-v1compare", "dev,v1compare,v1agents")
        if args.ui_tests:
            subprocess.run([str(venv_python(REPO / ".venv")), "-m", "playwright", "install", "chromium"],
                           check=True)
    prepare_local_files(refresh_launcher=args.refresh_launcher)


if __name__ == "__main__":
    main()
