#!/usr/bin/env python3
"""One-command project initialization entry point."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
VENV_SETUP = SCRIPTS_DIR / "setup_venv.py"
DATA_SETUP = SCRIPTS_DIR / "download_raw_data.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="研究プロジェクトの Python 環境と生データを初期化します。"
    )
    parser.add_argument(
        "--recreate-venv",
        action="store_true",
        help="既存の .venv を削除して再作成します。",
    )
    parser.add_argument(
        "--skip-dependencies",
        action="store_true",
        help="requirements.txt のインストールを省略します。",
    )
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="生データのダウンロードを省略します。",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=("n02", "s12", "population", "ekidata"),
        help="取得するデータ。複数回指定可。省略時は n02、s12、population。",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="既存の対象生データを再ダウンロードして置換します。",
    )
    parser.add_argument(
        "--ekidata-cookie",
        default=os.environ.get("EKIDATA_COOKIE"),
        metavar="COOKIE",
        help="駅データ.jp のログイン Cookie。環境変数でも指定できます。",
    )
    return parser.parse_args()


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("\n[INIT] " + " ".join(command), flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def initialized_python() -> Path:
    if sys.platform == "win32":
        return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def setup_venv(args: argparse.Namespace) -> None:
    command = [sys.executable, str(VENV_SETUP)]
    if args.recreate_venv:
        command.append("--recreate")
    if args.skip_dependencies:
        command.append("--skip-install")
    run(command)


def setup_data(args: argparse.Namespace) -> None:
    if args.skip_data:
        print("\n[SKIP] 生データの初期化を省略しました。")
        return

    command = [str(initialized_python()), str(DATA_SETUP)]
    for dataset in args.dataset or ():
        command.extend(("--dataset", dataset))
    if args.force_download:
        command.append("--force")

    environment = os.environ.copy()
    if args.ekidata_cookie:
        environment["EKIDATA_COOKIE"] = args.ekidata_cookie
    run(command, env=environment)


def main() -> int:
    args = parse_args()
    try:
        setup_venv(args)
        setup_data(args)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"\n[ERROR] 初期化に失敗しました: {exc}", file=sys.stderr)
        return 1

    print("\n初期化が完了しました。")
    if sys.platform == "win32":
        print(r"仮想環境を有効化: .venv\Scripts\activate")
    else:
        print("仮想環境を有効化: source .venv/bin/activate")
    print("DB を作成: python -m rail_data.build.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
