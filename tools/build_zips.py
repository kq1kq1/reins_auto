"""
配布用zip（管理者版・メンバー版）を作り直す。

方針:
  - 除外は一切しない。フォルダにあるものをそのまま入れる。
    （過去に __pycache__ を除外して、実績のある配布物と中身が変わる事故を起こした）
  - 配布してはいけないファイル（ログ・DB・ブラウザプロファイル）は
    フォルダ側に置かない運用とし、ここでは混入していないか検査するだけにする。
  - 検証に通ってから既存zipを置き換える。失敗したら既存zipは無傷のまま。

使い方:
    python tools/build_zips.py
"""

import hashlib
import os
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "reins_portable"
JOBS = [("REINS_member", "配布_メンバー版.zip"),
        ("REINS物件監視_管理者版", "配布_管理者版.zip")]

# 入っていてはいけないもの
# 注意: 同梱ライブラリ配下の __pycache__ は「入っているのが正しい」。
#       除くべきは app/ 直下のもの（実行時に生成される古いバイトコード）だけ。
FORBIDDEN = ["app/__pycache__", "browser_profile", "reins_auto.log",
             "reins_db.xlsx", "state.json", "debug_", "config.json.bak"]

# ソースと一致していることを確認する主要ファイル
KEY_FILES = ["app/monitor.py", "app/processor.py", "app/scraper.py",
             "app/sheets_backend.py", "app/pg_backend.py"]


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    for folder_name, zip_name in JOBS:
        folder = BASE / folder_name
        dst, tmp = BASE / zip_name, BASE / (zip_name + ".new")

        files, dirs = [], []
        for root, ds, fs in os.walk(folder):
            for d in ds:
                dirs.append((Path(root) / d).relative_to(folder.parent).as_posix() + "/")
            for fn in fs:
                p = Path(root) / fn
                files.append((p, p.relative_to(folder.parent).as_posix()))
        files.sort(key=lambda x: x[1])
        total = sum(p.stat().st_size for p, _ in files)

        print(f"\n=== {zip_name} ===", flush=True)
        print(f"  対象 {len(files):,}ファイル / {len(dirs):,}ディレクトリ / "
              f"{total / 1024**3:.2f}GB", flush=True)

        t0 = time.time()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for d in sorted(dirs):
                z.writestr(zipfile.ZipInfo(d), b"")
            for i, (p, arc) in enumerate(files, 1):
                z.write(p, arc)
                if i % 2500 == 0:
                    print(f"  ... {i:,}/{len(files):,} ({time.time() - t0:.0f}秒)", flush=True)
        print(f"  作成 {tmp.stat().st_size / 1024**3:.2f}GB ({time.time() - t0:.0f}秒)", flush=True)

        # ---- 検証（通らなければ既存zipを残したまま中断）----
        errors = []
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
            zfiles = {n for n in names if not n.endswith("/")}
            if zfiles != {a for _, a in files}:
                errors.append(f"ファイル一覧が現物と不一致 "
                              f"(zip {len(zfiles)} / 現物 {len(files)})")
            if {n.split("/")[0] for n in names} != {folder_name}:
                errors.append("最上位フォルダが不正")
            for rel in KEY_FILES:
                arc = f"{folder_name}/{rel}"
                if arc not in zfiles:
                    errors.append(f"欠落: {arc}")
                elif hashlib.md5(z.read(arc)).hexdigest() != md5(folder / rel):
                    errors.append(f"内容不一致: {arc}")
            for ng in FORBIDDEN:
                hit = [n for n in zfiles if ng in n]
                if hit:
                    errors.append(f"混入: {ng} ({len(hit)}件)")

        if errors:
            print("  ✘ 検証失敗（既存zipは変更していません）", flush=True)
            for e in errors:
                print(f"      - {e}", flush=True)
            tmp.unlink(missing_ok=True)
            sys.exit(1)

        print(f"  ✔ 検証OK  ファイル{len(zfiles):,} / 主要{len(KEY_FILES)}ファイル一致", flush=True)
        dst.unlink(missing_ok=True)
        tmp.rename(dst)
        print(f"  ✔ 置き換え完了 {dst.stat().st_size / 1024**3:.2f}GB", flush=True)

    print("\n両方の配布zipを作り直しました。", flush=True)


if __name__ == "__main__":
    main()
