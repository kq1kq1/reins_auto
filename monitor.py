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
    restore_candidates, confirm_removals, cleanup_db, STATUS_CANDIDATE,
    load_state, save_state,
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
    "daily", "weekly", "restore", "confirm", "cleanup",
    "half_morning", "half_daily", "half_weekly",
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

    # 既存DBから物件番号セットを取得（PDF DL対象を絞るため）
    existing_db = load_db(db_path)
    existing_ids, existing_no_zumen_ids = _get_existing_id_sets(existing_db)

    # ブラウザ起動・複数条件をループで取得
    scraper = REINSScraper(cfg)
    scraped_by_condition = await scraper.run_manual_loop(
        dl_zumen=dl_zumen,
        existing_ids=existing_ids,
        existing_no_zumen_ids=existing_no_zumen_ids,
    )

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

    db_df, archive_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str, archive_df=archive_df)

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
        _handle_pdfs(diff["new"] + diff.get("zumen_added", []), all_scraped, export_dir, print_cfg)

    # メール通知
    diff_for_mail = {
        "new":           diff["new"],
        "price_changed": diff["price_changed"],
        "candidates":    candidates,        # 今回新たに取消候補入りしたもの
        "confirmed":     confirmed,         # 猶予切れで成約・取消確定したもの
        "restored":      diff["restored"],
        "zumen_added":   diff.get("zumen_added", []),
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
    mode = 'half_morning' / 'half_daily' / 'half_weekly'
      half_morning: ログイン手動・各条件を「日付を指定（前日〜当日）」で自動検索
      half_daily  : ログイン手動・各条件を「当日」フィルタで自動検索
      half_weekly : ログイン手動・各条件を日付フィルタなしで自動検索 + 取消候補マーキング
    """
    # 週次は専用条件があればそれを使う（なければ通常条件）
    if mode == "half_weekly" and cfg.get("weekly_search_conditions"):
        search_conditions = cfg["weekly_search_conditions"]
    else:
        search_conditions = cfg["search_conditions"]

    db_path     = cfg["storage"]["db_path"]
    export_dir  = cfg["storage"]["export_dir"]
    keep_days   = cfg["storage"].get("pdf_keep_days", 7)
    grace_days  = cfg["storage"].get("removal_grace_days", 3)
    state_path  = cfg["storage"].get("state_path", "state.json")
    print_cfg   = cfg.get("print", {})

    cleanup_old_exports(export_dir, days=keep_days)

    today   = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not search_conditions:
        print("config.json の search_conditions が空です。条件を登録してください。")
        return

    ans = input("図面PDFをダウンロードして印刷しますか? [y/N]: ").strip().lower()
    dl_zumen = ans == "y"

    # 既存DBから物件番号セット（+ 図面なしIDセット）を取得
    existing_db = load_db(db_path)
    existing_ids, existing_no_zumen_ids = _get_existing_id_sets(existing_db)

    # 半自動朝のみ：前回実行日からの範囲を計算
    state = load_state(state_path)
    from_date_override = None
    if mode == "half_morning":
        last = state.get("half_morning_last_run", "")
        if last:
            try:
                from_date_override = datetime.strptime(last, "%Y-%m-%d")
                logger.info(f"前回半自動朝実行日: {last} → 今日までの範囲で検索")
            except ValueError:
                logger.warning(f"state内の日付形式が不正: {last}（前日にフォールバック）")

    scrape_mode = {
        "half_morning": "morning",
        "half_daily":   "evening",
        "half_weekly":  "weekly",
    }.get(mode, "evening")
    scraper = REINSScraper(cfg)
    scraped_by_condition = await scraper.run_after_login(
        search_conditions, run_mode=scrape_mode, dl_zumen=dl_zumen,
        existing_ids=existing_ids,
        existing_no_zumen_ids=existing_no_zumen_ids,
        from_date_override=from_date_override,
    )

    if not any(props for _, props in scraped_by_condition):
        print("全条件で取得件数0件。終了します。")
        return

    cleaned = [(cond, apply_rules(props)) for cond, props in scraped_by_condition]

    db_df      = load_db(db_path)
    archive_df = load_archive(db_path)

    db_df, archive_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str, archive_df=archive_df)

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

    # 半自動朝が成功したら state を今日に更新
    if mode == "half_morning":
        state["half_morning_last_run"] = today
        save_state(state_path, state)

    # グループ化された新規物件は最安1件だけを通知/印刷対象に絞る
    # グループ化は merge_batch で全条件まとめて済んでいる（条件をまたいで一意なグループID）
    new_filtered = _filter_cheapest_per_group(diff["new"])

    # 週次：取消候補で「グループの最安だった」物件がいたら、次の最安を号棟チェンジとして通知
    ridge_change: list[dict] = []
    if mode == "half_weekly":
        ridge_change = _detect_ridge_change(candidates, db_df)

    if dl_zumen:
        all_scraped = [p for _, props in cleaned for p in props]
        _handle_pdfs(new_filtered + diff.get("zumen_added", []), all_scraped, export_dir, print_cfg)

    diff_for_mail = {
        "new":           new_filtered,
        "price_changed": diff["price_changed"],
        "candidates":    candidates,
        "confirmed":     confirmed,
        "restored":      diff["restored"],
        "zumen_added":   diff.get("zumen_added", []),
        "ridge_change":  ridge_change,
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

import re

def _parse_prop_ids(s: str) -> list[str]:
    """カンマ・空白・改行で区切った物件番号文字列をリストに分解する。"""
    return [x for x in re.split(r"[,\s]+", s) if x.strip()]


def run_restore(cfg: dict) -> None:
    db_path = cfg["storage"]["db_path"]
    print()
    print("取消候補にある物件番号を入力すると、アクティブに戻します。")
    print("複数件はカンマ・空白・改行で区切って入力可能。空Enterで終了。")
    while True:
        s = input("\n物件番号: ").strip()
        if not s:
            break
        ids = _parse_prop_ids(s)
        ok, not_found = restore_candidates(db_path, ids)
        print(f"  ✔ {ok}件 アクティブに戻しました")
        if not_found:
            print(f"  ✘ 見つからなかった: {', '.join(not_found)}")


def run_cleanup(cfg: dict) -> None:
    """既存DBのクリーンアップ（グループID再計算 + identity重複統合）。"""
    import shutil

    db_path = cfg["storage"]["db_path"]
    if not Path(db_path).exists():
        print(f"DBファイルが見つかりません: {db_path}")
        return

    # バックアップを作成
    backup = Path(db_path).with_suffix(f".backup_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    shutil.copy2(db_path, backup)
    print(f"バックアップ作成: {backup.name}")
    print()

    print("クリーンアップを実行します...")
    stats = cleanup_db(db_path)

    print()
    print("=" * 60)
    print("クリーンアップ完了")
    print(f"  アクティブ件数: {stats['active_before']} → {stats['active_after']}")
    print(f"  重複統合(アーカイブ送り): {stats['merged']}件")
    print(f"  グループID付与: {stats['regrouped_count']}件")
    print(f"  バックアップ: {backup}")
    print("=" * 60)


def run_confirm(cfg: dict) -> None:
    db_path = cfg["storage"]["db_path"]
    print()
    print("物件番号を入力すると、物件DBから外し成約・取消シートへ移します。")
    print("複数件はカンマ・空白・改行で区切って入力可能。空Enterで終了。")
    while True:
        s = input("\n物件番号: ").strip()
        if not s:
            break
        ids = _parse_prop_ids(s)
        ok, not_found = confirm_removals(db_path, ids)
        print(f"  ✔ {ok}件 成約・取消シートへ移しました")
        if not_found:
            print(f"  ✘ 見つからなかった: {', '.join(not_found)}")


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

    db_df, archive_df, diff, log_rows = merge_batch(db_df, cleaned, today, now_str, archive_df=archive_df)

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
        _handle_pdfs(diff["new"] + diff.get("zumen_added", []), all_scraped, export_dir, print_cfg)

    if mode == "debug":
        _print_debug(diff, candidates, confirmed)
        return

    diff_for_mail = {
        "new":           diff["new"],
        "price_changed": diff["price_changed"],
        "candidates":    candidates,
        "confirmed":     confirmed,
        "restored":      diff["restored"],
        "zumen_added":   diff.get("zumen_added", []),
    }
    has_change = any(diff_for_mail[k] for k in diff_for_mail)
    if has_change or cfg["notification"].get("send_daily_summary"):
        total = (db_df["状態"] == "アクティブ").sum() if not db_df.empty else 0
        subject, body = build_summary_email(diff_for_mail, total=int(total), mode=mode)
        send_email(cfg["notification"], subject, body)


# ================================================================
# 共通ユーティリティ
# ================================================================

def _price_num(s) -> float:
    """価格文字列から数値を取り出す（取れなければ無限大）。"""
    if s is None:
        return float("inf")
    m = re.search(r"(\d+(?:\.\d+)?)", str(s).replace(",", ""))
    return float(m.group(1)) if m else float("inf")


def _filter_cheapest_per_group(new_props: list[dict]) -> list[dict]:
    """
    グループID付き物件は最安1件だけ残す（グループID無しは全件残す）。
    呼び出し前に必ず _regroup_globally() でクロスコンディションのグループ再計算を行うこと。
    """
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    ungrouped: list[dict] = []
    for p in new_props:
        gid = (p.get("グループID") or "").strip()
        if gid:
            groups[gid].append(p)
        else:
            ungrouped.append(p)

    result = list(ungrouped)
    for items in groups.values():
        cheapest = min(items, key=lambda p: _price_num(p.get("価格", "")))
        result.append(cheapest)
    return result


def _regroup_globally(props: list[dict]) -> list[dict]:
    """
    新規物件全体（複数条件をまたいで集計済み）を会社名+所在地丁目+徒歩分で
    再グループ化する。条件ごとの古いグループIDは破棄。
    """
    from rules import _mark_same_site_groups
    for p in props:
        p["グループID"] = ""
    _mark_same_site_groups(props)
    return props


def _detect_ridge_change(candidates: list[dict], db_df) -> list[dict]:
    """
    取消候補となった物件と同じ「会社名+丁目+沿線駅+徒歩分」グループの中で、
    まだアクティブな最安物件を「号棟チェンジ候補」として返す。
    """
    if db_df is None or db_df.empty or not candidates:
        return []
    from rules import _chome, _walk_min, _station

    active_mask = db_df["状態"].astype(str).str.strip() == "アクティブ"
    active_df = db_df[active_mask]
    if active_df.empty:
        return []

    def keyfn(rec):
        company = (rec.get("会社名") or "").strip()
        chome = _chome(rec.get("所在地", ""))
        walk = _walk_min(rec.get("交通", ""))
        station = _station(rec.get("沿線駅", "") or rec.get("交通", ""))
        if not company or not chome or not walk:
            return None
        return (company, chome, station, walk)

    seen_keys: set[tuple] = set()
    result: list[dict] = []
    for c in candidates:
        key = keyfn(c)
        if key is None or key in seen_keys:
            continue
        seen_keys.add(key)

        same_group = []
        for rec in active_df.to_dict("records"):
            if keyfn(rec) == key:
                same_group.append(rec)
        if not same_group:
            continue

        cheapest = min(same_group, key=lambda p: _price_num(p.get("価格", "")))
        result.append(cheapest)
    return result


def _get_existing_id_sets(db_df) -> tuple[set[str], set[str]]:
    """既存DBから 全物件IDセット と 図面なしIDセット を取得。"""
    existing_ids: set[str] = set()
    no_zumen_ids: set[str] = set()
    if db_df is None or db_df.empty or "物件番号" not in db_df.columns:
        return existing_ids, no_zumen_ids
    existing_ids = set(db_df["物件番号"].astype(str).str.strip())
    if "図面" in db_df.columns:
        mask = db_df["図面"].astype(str).str.strip() == "なし"
        no_zumen_ids = set(db_df.loc[mask, "物件番号"].astype(str).str.strip())
    return existing_ids, no_zumen_ids


def _handle_pdfs(new_props: list[dict], all_scraped: list[dict], export_dir: str, print_cfg: dict | None = None) -> None:
    """新規物件のPDFだけ残して結合・印刷。それ以外は削除する。
    同じ物件番号のPDFが複数回DLされていた場合は1つだけ採用し、残りは削除する。"""
    new_ids  = {p.get("物件番号") for p in new_props}
    pid_to_pdf: dict[str, Path] = {}

    for prop in all_scraped:
        pdf_path = prop.get("_pdf_path", "")
        if not pdf_path:
            continue
        p = Path(pdf_path)
        pid = prop.get("物件番号")
        if pid in new_ids:
            if pid in pid_to_pdf:
                # 重複：すでに登録済みなのでこのファイルは削除
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                pid_to_pdf[pid] = p
        else:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    if not pid_to_pdf:
        return

    merged = merge_pdfs(list(pid_to_pdf.values()), Path(export_dir))
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
    elif mode == "confirm":
        run_confirm(cfg)
    elif mode == "cleanup":
        run_cleanup(cfg)
    elif mode in ("daily", "weekly"):
        asyncio.run(run_loop(cfg, mode))
    elif mode in ("half_morning", "half_daily", "half_weekly"):
        asyncio.run(run_half_auto(cfg, mode))
    else:
        asyncio.run(run_auto(mode, cfg))

    logger.info("=== 完了 ===")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
