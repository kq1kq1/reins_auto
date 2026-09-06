"""
既存のスプレッドシートの内容を Postgres(Supabase) へ初回投入する（1回だけ実行）。

スプレッドシートからは読み取るだけで、書き込みは一切しない。
何度実行しても UPSERT なので二重登録にならない（変更ログを除く）。

使い方:
    python tools/backfill_pg.py            # 物件のみ投入
    python tools/backfill_pg.py --logs     # 変更ログも投入（重複するので初回だけ）
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import processor          # noqa: E402
import pg_backend         # noqa: E402


def main():
    with_logs = "--logs" in sys.argv

    cfg_all = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    pg_cfg = cfg_all.get("postgres") or {}
    if not pg_cfg.get("dsn"):
        print("config.json に postgres.dsn がありません。")
        sys.exit(1)

    processor.configure_storage(cfg_all["storage"])
    db_path = cfg_all["storage"]["db_path"]

    print("=" * 58)
    print(" スプレッドシート → Supabase 初回投入")
    print("=" * 58)

    # ── 読み込み（スプシへは読み取りのみ）──
    t = time.time()
    db_df = processor.load_db(db_path)
    archive_df = processor.load_archive(db_path)
    print(f"[1] スプシ読み込み  物件DB {len(db_df):,}行 / 成約・取消 {len(archive_df):,}行"
          f"  ({time.time() - t:.1f}秒)")

    # ── 変換の下見（実際に書く前に、数値化の成否を確認する）──
    rows, dup = pg_backend.build_rows(db_df, archive_df)
    idx = {c: i for i, c in enumerate(pg_backend.DB_COLUMNS)}
    total = len(rows)
    ok_price = sum(1 for r in rows if r[idx["price"]] is not None)
    ok_walk = sum(1 for r in rows if r[idx["walk_min"]] is not None)
    ok_year = sum(1 for r in rows if r[idx["built_year"]] is not None)
    ok_ident = sum(1 for r in rows if r[idx["identity_key"]])
    print(f"[2] 変換            {total:,}行（物件番号の重複除外 {dup}件）")
    print(f"      価格の数値化   {ok_price:,}/{total:,} ({ok_price / total:.0%})")
    print(f"      徒歩分の抽出   {ok_walk:,}/{total:,} ({ok_walk / total:.0%})")
    print(f"      築年の抽出     {ok_year:,}/{total:,} ({ok_year / total:.0%})")
    print(f"      identityキー   {ok_ident:,}/{total:,} ({ok_ident / total:.0%})")
    print("      ※数値化できなかった分も、原文は raw 列に残っています")

    # ── 投入 ──
    t = time.time()
    n = pg_backend.upsert_properties(pg_cfg, db_df, archive_df)
    print(f"[3] 物件を投入      {n:,}行  ({time.time() - t:.1f}秒)")

    if with_logs:
        log_df = processor.load_log(db_path) if hasattr(processor, "load_log") else None
        if log_df is not None and not log_df.empty:
            t = time.time()
            m = pg_backend.append_logs(pg_cfg, log_df.to_dict("records"))
            print(f"[4] 変更ログを投入  {m:,}行  ({time.time() - t:.1f}秒)")
        else:
            print("[4] 変更ログ        読み込めなかったのでスキップ")

    # ── 結果確認 ──
    c = pg_backend.counts(pg_cfg)
    print("-" * 58)
    print(f"Postgres の中身      合計 {c['total']:,}行")
    for status, cnt in sorted(c["by_status"].items(), key=lambda x: -x[1]):
        print(f"    {status:<12} {cnt:,}件")
    print(f"    変更ログ       {c['change_log']:,}行")
    print("=" * 58)


if __name__ == "__main__":
    main()
