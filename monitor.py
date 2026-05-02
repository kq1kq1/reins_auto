"""
REINS自動監視 メインスクリプト

使い方:
  python monitor.py daily      # 毎日: ブラウザ1回・複数条件ループ・図面DL+印刷
  python monitor.py weekly     # 週次: 毎日と同じUI + 終了時に取消候補チェック
  python monitor.py restore    # 取消候補を物件番号指定でアクティブに戻す
  python monitor.py test_mail  # メール送信テスト

  以下は自動操作モード（規約上のリスクあり・通常は使わない）:
  python monitor.py morning    # 朝自動: 前日〜今日の新着検索
  python monitor.py evening    # 夕自動: 当日の新着検索
  python monitor.py auto_weekly  # 週次自動: 全件検索 → 取消検知
  python monitor.py bootstrap  # 初期DB構築: 全件取り込み（図面DLなし）
  python monitor.py debug      # ブラウザ表示ON・通知なし（セレクタ確認用）
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from scraper import REINSScraper
from processor import (
    load_db, load_archive, save_db,
    merge_batch, mark_removal_candidates, process_grace_period,
    restore_candidate, STATUS_CANDIDATE,
)
from rules import apply_rules
from mailer import send_email, build_summary_email
from pdf_handler import cleanup_old_exports, merge_pdfs, print_pdf


def setup_logging(log_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger(__name__)

VALID_MODES = (
    "daily", "weekly", "restore",
    "half_daily", "half_weekly",
    "morning", "evening", "auto_weekly", "bootstrap", "debug", "test_mail",
)


def load_config() -> dict:
    p = Path(__file__).parent / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))

    import os
    env_map = {
        "REINS_USER":          ("reins",        "username"),
        "REINS_PASS":          ("reins",        "password"),
        "REINS_EMAIL_FROM":    ("notification", "email_from"),
        "REINS_EMAIL_TO":      ("notification", "email_to"),
        "REINS_SMTP_PASSWORD": ("notification", "smtp_password"),
    }
    for var, (section, key) in env_map.items():
        val = os.environ.get(var)
        if val:
            cfg[section][key] = val
    return cfg


# ================================================================
# 毎日 / 週次（手動・ループ式）
# ================================================================

async def run_loop(cfg: dict, mode: str) -> None:
    """
    手動操作によるループ実行。
    mode = 'daily' or 'weekly'
      daily : マージのみ（取消候補マーキングなし）
      weekly: マージ後、見つからなかった物件を取消候補に
    """
    db_path     = cfg["storage"]["db_path"]
    export_dir  = cfg["storage"]["export_dir"]
    keep_days   = cfg["storage"].get("pdf_keep_days", 7)
    grace_days  = cfg["storage"].get("removal_grace_days", 3)
    print_cfg   = cfg.get("print", {})

    cleanup_old_exports(export_dir, days=keep_days)

    today   = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 図面DLするか確認
    ans = input("図面PDFをダウンロードして印刷しますか? [y/N]: ").strip().lower()
    dl_zumen = ans == "y"

    # ブラウザ起動・複数条件をループで取得
    scraper = REINSScraper(cfg)
    scraped_by_condition = await scraper.run_manual_loop(dl_zumen=dl_zumen)

    if not scraped_by_condition:
        print("条件が1つも入力されませんでした。終了します。")
        return

    # ルール適用（重複除外・グルーピング）
    cleaned: list[tuple[str, list[dict]]] = []
    for cond, props in scraped_by_condition:
        cleaned.append((cond, apply_rules(props)))

    # DB読み込み・マージ
    db_df      = load_db(db_path)
    archive_df = load_archive(db_path)

    db_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str)

    candidates: list[dict] = []
    if mode == "weekly":
        db_df, candidates, weekly_logs = mark_removal_candidates(
            db_df, diff["found_ids"], today, now_str
        )
        log_rows.extend(weekly_logs)

    # 猶予期間切れの取消候補を成約・取消へ
    db_df, archive_df, confirmed, grace_logs = process_grace_period(
        db_df, archive_df, today, now_str, grace_days=grace_days
    )
    log_rows.extend(grace_logs)

    save_db(db_path, db_df, archive_df, log_rows)

    # 図面PDFの処理（新規物件のみ印刷）
    if dl_zumen:
        all_scraped = [p for _, props in cleaned for p in props]
        _handle_pdfs(diff["new"], all_scraped, export_dir, print_cfg)

    # メール通知
    diff_for_mail = {
        "new":           diff["new"],
        "price_changed": diff["price_changed"],
        "candidates":    candidates,        # 今回新たに取消候補入りしたもの
        "confirmed":     confirmed,         # 猶予切れで成約・取消確定したもの
        "restored":      diff["restored"],
    }

    has_change = any(diff_for_mail[k] for k in diff_for_mail)
    if has_change or cfg["notification"].get("send_daily_summary"):
        total = (db_df["状態"] == "アクティブ").sum() if not db_df.empty else 0
        subject, body = build_summary_email(diff_for_mail, total=int(total), mode=mode)
        send_email(cfg["notification"], subject, body)

    # サマリ表示
    print()
    print("=" * 60)
    print(f"完了 [{mode}]")
    print(f"  新規:           {len(diff['new'])}件")
    print(f"  価格変更:       {len(diff['price_changed'])}件")
    print(f"  取消候補から復活: {len(diff['restored'])}件")
    if mode == "weekly":
        print(f"  新規取消候補:   {len(candidates)}件")
    print(f"  成約・取消確定: {len(confirmed)}件（猶予{grace_days}日経過）")
    print("=" * 60)


# ================================================================
# 半自動（手動ログイン後に全条件自動巡回）
# ================================================================

async def run_half_auto(cfg: dict, mode: str) -> None:
    """
    mode = 'half_daily' or 'half_weekly'
      half_daily : ログイン手動・各条件を「当日」フィルタで自動検索
      half_weekly: ログイン手動・各条件を日付フィルタなしで自動検索 + 取消候補マーキング
    """
    search_conditions = cfg["search_conditions"]
    db_path     = cfg["storage"]["db_path"]
    export_dir  = cfg["storage"]["export_dir"]
    keep_days   = cfg["storage"].get("pdf_keep_days", 7)
    grace_days  = cfg["storage"].get("removal_grace_days", 3)
    print_cfg   = cfg.get("print", {})

    cleanup_old_exports(export_dir, days=keep_days)

    today   = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not search_conditions:
        print("config.json の search_conditions が空です。条件を登録してください。")
        return

    ans = input("図面PDFをダウンロードして印刷しますか? [y/N]: ").strip().lower()
    dl_zumen = ans == "y"

    scrape_mode = "evening" if mode == "half_daily" else "weekly"
    scraper = REINSScraper(cfg)
    scraped_by_condition = await scraper.run_after_login(
        search_conditions, run_mode=scrape_mode, dl_zumen=dl_zumen,
    )

    if not any(props for _, props in scraped_by_condition):
        print("全条件で取得件数0件。終了します。")
        return

    cleaned = [(cond, apply_rules(props)) for cond, props in scraped_by_condition]

    db_df      = load_db(db_path)
    archive_df = load_archive(db_path)

    db_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str)

    candidates: list[dict] = []
    if mode == "half_weekly":
        db_df, candidates, weekly_logs = mark_removal_candidates(
            db_df, diff["found_ids"], today, now_str
        )
        log_rows.extend(weekly_logs)

    db_df, archive_df, confirmed, grace_logs = process_grace_period(
        db_df, archive_df, today, now_str, grace_days=grace_days
    )
    log_rows.extend(grace_logs)

    save_db(db_path, db_df, archive_df, log_rows)

    if dl_zumen:
        all_scraped = [p for _, props in cleaned for p in props]
        _handle_pdfs(diff["new"], all_scraped, export_dir, print_cfg)

    diff_for_mail = {
        "new":           diff["new"],
        "price_changed": diff["price_changed"],
        "candidates":    candidates,
        "confirmed":     confirmed,
        "restored":      diff["restored"],
    }
    has_change = any(diff_for_mail[k] for k in diff_for_mail)
    if has_change or cfg["notification"].get("send_daily_summary"):
        total = (db_df["状態"] == "アクティブ").sum() if not db_df.empty else 0
        subject, body = build_summary_email(diff_for_mail, total=int(total), mode=mode)
        send_email(cfg["notification"], subject, body)

    print()
    print("=" * 60)
    print(f"完了 [{mode}]")
    print(f"  新規:           {len(diff['new'])}件")
    print(f"  価格変更:       {len(diff['price_changed'])}件")
    print(f"  取消候補から復活: {len(diff['restored'])}件")
    if mode == "half_weekly":
        print(f"  新規取消候補:   {len(candidates)}件")
    print(f"  成約・取消確定: {len(confirmed)}件（猶予{grace_days}日経過）")
    print("=" * 60)


# ================================================================
# 戻す（取消候補 → アクティブ復元）
# ================================================================

def run_restore(cfg: dict) -> None:
    db_path = cfg["storage"]["db_path"]
    print()
    print("取消候補にある物件番号を入力すると、アクティブに戻します。")
    print("空Enterで終了します。")
    while True:
        pid = input("\n物件番号: ").strip()
        if not pid:
            break
        if restore_candidate(db_path, pid):
            print(f"  ✔ {pid} をアクティブに戻しました")
        else:
            print(f"  ✘ {pid} が見つかりません（または取消候補ではありません）")


# ================================================================
# 自動操作モード（規約リスクあり・通常使わない）
# ================================================================

async def run_auto(mode: str, cfg: dict) -> None:
    browser_cfg = dict(cfg.get("browser", {}))
    if mode == "debug":
        browser_cfg["headless"] = False

    search_conditions = cfg["search_conditions"]
    db_path     = cfg["storage"]["db_path"]
    export_dir  = cfg["storage"]["export_dir"]
    keep_days   = cfg["storage"].get("pdf_keep_days", 7)
    grace_days  = cfg["storage"].get("removal_grace_days", 3)
    print_cfg   = cfg.get("print", {})

    cleanup_old_exports(export_dir, days=keep_days)

    today   = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    scraper = REINSScraper({**cfg, "browser": browser_cfg})
    if mode == "bootstrap":
        scraper.skip_zumen = True
        scrape_mode = "weekly"
    elif mode == "auto_weekly":
        scrape_mode = "weekly"
    else:
        scrape_mode = mode

    results = await scraper.run(search_conditions, run_mode=scrape_mode)

    if not any(results.values()):
        logger.warning("全条件で取得件数0件")
        if mode not in ("debug", "bootstrap"):
            send_email(
                cfg["notification"],
                "[REINS自動監視] ⚠️ 取得件数0件",
                "<p>スクレイピング結果が0件でした。</p>",
            )
        return

    cleaned = [(name, apply_rules(props)) for name, props in results.items()]

    db_df      = load_db(db_path)
    archive_df = load_archive(db_path)

    db_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str)

    candidates: list[dict] = []
    if mode in ("auto_weekly",):
        db_df, candidates, weekly_logs = mark_removal_candidates(
            db_df, diff["found_ids"], today, now_str
        )
        log_rows.extend(weekly_logs)

    db_df, archive_df, confirmed, grace_logs = process_grace_period(
        db_df, archive_df, today, now_str, grace_days=grace_days
    )
    log_rows.extend(grace_logs)

    if mode == "bootstrap":
        save_db(db_path, db_df, archive_df, log_rows)
        print(f"\n初期DB構築完了: {len(db_df)}件")
        return

    save_db(db_path, db_df, archive_df, log_rows)

    if mode in ("morning", "evening"):
        all_scraped = [p for _, props in cleaned for p in props]
        _handle_pdfs(diff["new"], all_scraped, export_dir, print_cfg)

    if mode == "debug":
        _print_debug(diff, candidates, confirmed)
        return

    diff_for_mail = {
        "new":           diff["new"],
        "price_changed": diff["price_changed"],
        "candidates":    candidates,
        "confirmed":     confirmed,
        "restored":      diff["restored"],
    }
    has_change = any(diff_for_mail[k] for k in diff_for_mail)
    if has_change or cfg["notification"].get("send_daily_summary"):
        total = (db_df["状態"] == "アクティブ").sum() if not db_df.empty else 0
        subject, body = build_summary_email(diff_for_mail, total=int(total), mode=mode)
        send_email(cfg["notification"], subject, body)


# ================================================================
# 共通ユーティリティ
# ================================================================

def _handle_pdfs(new_props: list[dict], all_scraped: list[dict], export_dir: str, print_cfg: dict | None = None) -> None:
    """新規物件のPDFだけ残して結合・印刷。それ以外は削除する。"""
    new_ids  = {p.get("物件番号") for p in new_props}
    new_pdfs = []

    for prop in all_scraped:
        pdf_path = prop.get("_pdf_path", "")
        if not pdf_path:
            continue
        p = Path(pdf_path)
        if prop.get("物件番号") in new_ids:
            new_pdfs.append(p)
        else:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    if not new_pdfs:
        return

    merged = merge_pdfs(new_pdfs, Path(export_dir))
    if merged:
        print_pdf(merged, print_cfg)


def _print_debug(diff: dict, candidates: list[dict], confirmed: list[dict]) -> None:
    print(f"\n--- DEBUG サマリー ---")
    print(f"新規:           {len(diff['new'])}件")
    print(f"価格変更:       {len(diff['price_changed'])}件")
    print(f"取消候補から復活: {len(diff['restored'])}件")
    print(f"新規取消候補:   {len(candidates)}件")
    print(f"成約・取消確定: {len(confirmed)}件")
    if diff["new"]:
        print("\n新規物件（先頭5件）:")
        for p in diff["new"][:5]:
            grp = f" [{p.get('グループID')}]" if p.get("グループID") else ""
            print(f"  {p.get('所在地')} / {p.get('価格')}万円 / {p.get('間取り')}{grp}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    cfg  = load_config()
    setup_logging(cfg["storage"]["log_path"])

    logger.info(f"=== REINS自動監視開始 mode={mode} {datetime.now():%Y-%m-%d %H:%M} ===")

    if mode == "test_mail":
        diff = {
            "new": [{
                "物件番号": "TEST001", "会社名": "テスト不動産", "物件種別": "中古マンション",
                "所在地": "江戸川区西葛西5丁目", "交通": "西葛西駅 徒歩7分",
                "価格": "3480", "間取り": "3LDK", "専有面積": "72.4",
                "土地面積": "", "築年月": "2005年3月", "登録日": today_str(),
                "取引態様": "仲介", "グループID": "", "初回取得日": today_str(),
                "最終確認日": today_str(),
            }],
            "price_changed": [], "candidates": [], "confirmed": [], "restored": [],
        }
        subject, body = build_summary_email(diff, total=150, mode="daily")
        ok = send_email(cfg["notification"], subject, body)
        print("送信成功!" if ok else "送信失敗 → config.json のメール設定を確認")
        return

    if mode not in VALID_MODES:
        print(f"使い方: python monitor.py [{' | '.join(VALID_MODES)}]")
        sys.exit(1)

    if mode == "restore":
        run_restore(cfg)
    elif mode in ("daily", "weekly"):
        asyncio.run(run_loop(cfg, mode))
    elif mode in ("half_daily", "half_weekly"):
        asyncio.run(run_half_auto(cfg, mode))
    else:
        asyncio.run(run_auto(mode, cfg))

    logger.info("=== 完了 ===")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
