"""
Windowsタスクスケジューラに自動実行タスクを登録するスクリプト
管理者権限のコマンドプロンプトで実行してください:
  python setup_scheduler.py
"""

import subprocess
import sys
import json
from pathlib import Path


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()
    lines = [l for l in raw.splitlines() if not l.strip().startswith('"_comment')]
    return json.loads("\n".join(lines))


def register_task(task_name: str, python_exe: str, script_path: str,
                  mode: str, schedule_type: str, hour: int, minute: int,
                  day_of_week: str = None) -> None:
    cmd = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", f'"{python_exe}" "{script_path}" {mode}',
        "/SC", schedule_type,
        "/ST", f"{hour:02d}:{minute:02d}",
        "/F",
    ]
    if schedule_type == "WEEKLY" and day_of_week:
        day_map = {"monday":"MON","tuesday":"TUE","wednesday":"WED",
                   "thursday":"THU","friday":"FRI","saturday":"SAT","sunday":"SUN"}
        cmd += ["/D", day_map.get(day_of_week.lower(), "MON")]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="cp932")
    if result.returncode == 0:
        print(f"  ✅ {task_name} 登録成功")
    else:
        print(f"  ❌ {task_name} 登録失敗: {result.stderr.strip()}")


def main():
    cfg          = load_config()
    schedule_cfg = cfg.get("schedule", {})
    python_exe   = sys.executable
    script_path  = str(Path(__file__).parent / "monitor.py")

    print(f"Python  : {python_exe}")
    print(f"スクリプト: {script_path}")
    print()

    # 日次タスク: 毎日 process モードで実行（デフォルト 7:30）
    register_task(
        task_name    = "REINS_Monitor_Daily",
        python_exe   = python_exe,
        script_path  = script_path,
        mode         = "process",
        schedule_type= "DAILY",
        hour         = 7,
        minute       = 30,
    )
    print("  → 毎日 7:30 にダウンロードフォルダをチェックします\n")

    # 週次タスク: 月曜 weekly モード（成約検出あり）
    weekly_day = schedule_cfg.get("weekly_removed_check_day", "monday")
    register_task(
        task_name    = "REINS_Monitor_Weekly",
        python_exe   = python_exe,
        script_path  = script_path,
        mode         = "weekly",
        schedule_type= "WEEKLY",
        hour         = 8,
        minute       = 0,
        day_of_week  = weekly_day,
    )
    print(f"  → 毎週{weekly_day} 8:00 に成約・取消チェックも含めて実行します\n")

    print("登録完了! タスクスケジューラで確認: REINS_Monitor_Daily / REINS_Monitor_Weekly")


if __name__ == "__main__":
    main()
