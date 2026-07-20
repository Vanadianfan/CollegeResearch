#!/usr/bin/env python3
"""Create the project virtual environment and install its dependencies."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
MINIMUM_PYTHON = (3, 11)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=".venv を作成し、requirements.txt をインストールします。"
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="既存の .venv を削除して作り直します。",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="仮想環境だけを用意し、依存パッケージのインストールを省略します。",
    )
    return parser.parse_args()


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_supported_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(map(str, MINIMUM_PYTHON))
        current = ".".join(map(str, sys.version_info[:3]))
        raise RuntimeError(
            f"Python {required} 以上が必要です（現在: Python {current}）。"
        )


def recreate_venv() -> None:
    if not VENV_DIR.exists():
        return
    if Path(sys.prefix).resolve() == VENV_DIR.resolve():
        raise RuntimeError(
            "実行中の .venv 自体は再作成できません。deactivate 後、"
            "システムの python3 で setup.py を実行してください。"
        )
    print(f"[REMOVE] {VENV_DIR.relative_to(PROJECT_ROOT)}")
    shutil.rmtree(VENV_DIR)


def create_venv() -> None:
    python = venv_python()
    if python.is_file():
        print(f"[SKIP] .venv は既に存在します: {python}", flush=True)
        return
    if VENV_DIR.exists():
        raise RuntimeError(
            ".venv は存在しますが Python 実行ファイルがありません。"
            "--recreate を指定してください。"
        )
    print(f"[CREATE] {VENV_DIR.relative_to(PROJECT_ROOT)}", flush=True)
    venv.EnvBuilder(with_pip=True).create(VENV_DIR)


def run(command: list[str]) -> None:
    print("[RUN] " + " ".join(command), flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def install_dependencies() -> None:
    if not REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"依存関係ファイルがありません: {REQUIREMENTS_FILE}")
    python = str(venv_python())
    run([python, "-m", "pip", "install", "--requirement", str(REQUIREMENTS_FILE)])
    run([python, "-m", "pip", "check"])


def main() -> int:
    args = parse_args()
    try:
        ensure_supported_python()
        if args.recreate:
            recreate_venv()
        create_venv()
        if not args.skip_install:
            install_dependencies()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Python: {venv_python()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
