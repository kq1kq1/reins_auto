"""
差分検出・DB保存モジュール（統合DB版）

シート構成:
  物件DB        ... 全条件横断のアクティブ/取消候補物件を1シートで管理
  成約・取消     ... 取消確定物件のアーカイブ
  変更ログ       ... 全変更履歴
"""

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment

logger = logging.getLogger(__name__)

SHEET_DB      = "物件DB"
SHEET_REMOVED = "成約・取消"
SHEET_LOG     = "変更ログ"
ID_COL        = "物件番号"
PRICE_COL     = "価格"

STATUS_ACTIVE    = "アクティブ"
STATUS_CANDIDATE = "取消候補"

COLUMNS = [
    "物件番号", "物件種別", "取引状況", "取引態様",
    "所在地", "建物名", "所在階", "間取り",
    "専有面積", "建物面積", "土地面積",
    "価格", "㎡単価", "坪単価", "管理費",
    "用途地域", "建ぺい率", "容積率", "接道状況", "接道１",
    "沿線駅", "交通", "築年月",
    "会社名", "電話番号",
    "登録日", "図面", "検出条件", "状態", "取消候補日",
    "グループID", "初回取得日", "最終確認日",
]
REMOVED_COLUMNS = COLUMNS + ["成約・取消日"]
LOG_COLUMNS     = ["日時", "検索条件名", "変更", "物件番号", "所在地", "価格", "旧価格"]


# ----------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------

def load_db(db_path: str) -> pd.DataFrame:
    """物件DBシートを読み込む。なければ空のDFを返す。"""
    if not Path(db_path).exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        xl = pd.ExcelFile(db_path)
        if SHEET_DB not in xl.sheet_names:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.read_excel(xl, sheet_name=SHEET_DB, dtype=str).fillna("")
        # 旧形式DBに新カラムが無い場合は追加
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    except Exception as e:
        logger.error(f"DB読込エラー: {e}")
        return pd.DataFrame(columns=COLUMNS)


def load_archive(db_path: str) -> pd.DataFrame:
    """成約・取消シートを読み込む。"""
    if not Path(db_path).exists():
        return pd.DataFrame(columns=REMOVED_COLUMNS)
    try:
        xl = pd.ExcelFile(db_path)
        if SHEET_REMOVED not in xl.sheet_names:
            return pd.DataFrame(columns=REMOVED_COLUMNS)
        df = pd.read_excel(xl, sheet_name=SHEET_REMOVED, dtype=str).fillna("")
        for col in REMOVED_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[REMOVED_COLUMNS]
    except Exception as e:
        logger.error(f"アーカイブ読込エラー: {e}")
        return pd.DataFrame(columns=REMOVED_COLUMNS)


def load_log(db_path: str) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame(columns=LOG_COLUMNS)
    try:
        xl = pd.ExcelFile(db_path)
        if SHEET_LOG not in xl.sheet_names:
            return pd.DataFrame(columns=LOG_COLUMNS)
        return pd.read_excel(xl, sheet_name=SHEET_LOG, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=LOG_COLUMNS)


# ----------------------------------------------------------------
# バッチマージ（1回の実行で取れた全条件分の物件をDBに反映）
# ----------------------------------------------------------------

def merge_batch(
    db_df: pd.DataFrame,
    scraped_by_condition: list[tuple[str, list[dict]]],
    today: str,
    now_str: str,
) -> tuple[pd.DataFrame, dict, list[dict]]:
    """
    複数条件分のスクレイプ結果をDBにマージする。
    Returns:
      - 更新後のdb_df
      - diff: {new, price_changed, restored, found_ids}
      - new_log_rows: 変更ログに追加する行
    """
    diff = {"new": [], "price_changed": [], "restored": [], "zumen_added": [], "found_ids": set()}
    log_rows: list[dict] = []

    # 既存物件を物件番号でインデックス化
    db_records: dict[str, dict] = {}
    if not db_df.empty:
        for rec in db_df.to_dict("records"):
            pid = str(rec.get(ID_COL, "")).strip()
            if pid:
                db_records[pid] = rec

    for condition_name, props in scraped_by_condition:
        for prop in props:
            pid = str(prop.get(ID_COL, "")).strip()
            if not pid:
                continue
            diff["found_ids"].add(pid)

            if pid in db_records:
                rec = db_records[pid]
                old_status = rec.get("状態", "")
                old_price  = rec.get(PRICE_COL, "")
                new_price  = prop.get(PRICE_COL, "")
                old_zumen  = rec.get("図面", "")
                new_zumen  = prop.get("図面", "")

                # 既存物件 → 最終確認日更新・状態リセット・検出条件マージ
                rec["最終確認日"] = today
                rec["状態"]        = STATUS_ACTIVE
                rec["取消候補日"] = ""
                rec["検出条件"]   = _merge_conditions(rec.get("検出条件", ""), condition_name)
                if new_zumen:
                    rec["図面"] = new_zumen

                # 価格変更検出
                if old_price and new_price and _norm_price(old_price) != _norm_price(new_price):
                    rec[PRICE_COL] = new_price
                    p = dict(prop)
                    p["旧価格"] = old_price
                    p["新価格"] = new_price
                    diff["price_changed"].append(p)
                    log_rows.append(_log_row(now_str, condition_name, "価格変更", prop, old_price))

                # 取消候補からの復活
                if old_status == STATUS_CANDIDATE:
                    diff["restored"].append(prop)
                    log_rows.append(_log_row(now_str, condition_name, "取消候補から復活", prop))

                # 図面が「なし」→「あり」に変わった
                if old_zumen == "なし" and new_zumen == "あり":
                    diff["zumen_added"].append(prop)
                    log_rows.append(_log_row(now_str, condition_name, "図面追加", prop))
            else:
                # 新規物件
                new_rec = {col: prop.get(col, "") for col in COLUMNS}
                new_rec["検出条件"]   = condition_name
                new_rec["状態"]        = STATUS_ACTIVE
                new_rec["取消候補日"] = ""
                new_rec["初回取得日"] = today
                new_rec["最終確認日"] = today
                db_records[pid] = new_rec
                diff["new"].append(prop)
                log_rows.append(_log_row(now_str, condition_name, "新規登録", prop))

    new_df = pd.DataFrame(list(db_records.values()), columns=COLUMNS)
    logger.info(
        f"マージ完了 → 新規:{len(diff['new'])} "
        f"価格変更:{len(diff['price_changed'])} 復活:{len(diff['restored'])}"
    )
    return new_df, diff, log_rows


# ----------------------------------------------------------------
# 週次: 取消候補マーキング
# ----------------------------------------------------------------

def mark_removal_candidates(
    db_df: pd.DataFrame, found_ids: set[str], today: str, now_str: str,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """
    今回の週次実行で見つからなかった「アクティブ」物件を「取消候補」にする。
    Returns:
      - 更新後のdb_df
      - candidates: 新たに取消候補になった物件dictのリスト
      - log_rows: 変更ログ行
    """
    candidates: list[dict] = []
    log_rows: list[dict]   = []
    if db_df.empty:
        return db_df, candidates, log_rows

    records = db_df.to_dict("records")
    for rec in records:
        pid    = str(rec.get(ID_COL, "")).strip()
        status = rec.get("状態", "")
        if not pid or status != STATUS_ACTIVE:
            continue
        if pid not in found_ids:
            rec["状態"]        = STATUS_CANDIDATE
            rec["取消候補日"] = today
            candidates.append(dict(rec))
            log_rows.append(_log_row(now_str, rec.get("検出条件", ""), "取消候補", rec))

    new_df = pd.DataFrame(records, columns=COLUMNS)
    logger.info(f"取消候補マーキング: {len(candidates)}件")
    return new_df, candidates, log_rows


# ----------------------------------------------------------------
# 猶予期間切れ: 取消候補を成約・取消シートへ移動
# ----------------------------------------------------------------

def process_grace_period(
    db_df: pd.DataFrame,
    archive_df: pd.DataFrame,
    today: str,
    now_str: str,
    grace_days: int = 3,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict], list[dict]]:
    """
    取消候補日から grace_days 経過した物件を成約・取消シートへ移動する。
    Returns:
      - 更新後のdb_df（移動した物件は除去）
      - 更新後のarchive_df（移動した物件を追加）
      - confirmed: 成約・取消が確定した物件dictリスト
      - log_rows: 変更ログ行
    """
    confirmed: list[dict] = []
    log_rows: list[dict]   = []
    if db_df.empty:
        return db_df, archive_df, confirmed, log_rows

    today_dt = datetime.strptime(today, "%Y-%m-%d")
    keep_records: list[dict] = []
    archive_records = archive_df.to_dict("records") if not archive_df.empty else []

    for rec in db_df.to_dict("records"):
        if rec.get("状態", "") != STATUS_CANDIDATE:
            keep_records.append(rec)
            continue
        cdate = str(rec.get("取消候補日", "")).strip()
        if not cdate:
            keep_records.append(rec)
            continue
        try:
            cdate_dt = datetime.strptime(cdate, "%Y-%m-%d")
        except ValueError:
            keep_records.append(rec)
            continue
        if today_dt - cdate_dt >= timedelta(days=grace_days):
            arch = {col: rec.get(col, "") for col in COLUMNS}
            arch["成約・取消日"] = today
            archive_records.append(arch)
            confirmed.append(dict(rec))
            log_rows.append(_log_row(now_str, rec.get("検出条件", ""), "成約・取消確定", rec))
        else:
            keep_records.append(rec)

    new_db_df      = pd.DataFrame(keep_records,    columns=COLUMNS)
    new_archive_df = pd.DataFrame(archive_records, columns=REMOVED_COLUMNS)
    if confirmed:
        logger.info(f"成約・取消確定: {len(confirmed)}件（猶予{grace_days}日経過）")
    return new_db_df, new_archive_df, confirmed, log_rows


# ----------------------------------------------------------------
# 取消候補 → アクティブ 復元（戻す.bat 用）
# ----------------------------------------------------------------

def restore_candidate(db_path: str, prop_id: str) -> bool:
    """指定した物件番号の状態を取消候補→アクティブに戻す。"""
    db_df = load_db(db_path)
    if db_df.empty:
        return False
    mask = db_df[ID_COL].astype(str).str.strip() == str(prop_id).strip()
    if not mask.any():
        return False
    db_df.loc[mask, "状態"]       = STATUS_ACTIVE
    db_df.loc[mask, "取消候補日"] = ""
    save_db(db_path, db_df, load_archive(db_path), [])
    return True


# ----------------------------------------------------------------
# 保存
# ----------------------------------------------------------------

def save_db(
    db_path: str,
    db_df: pd.DataFrame,
    archive_df: pd.DataFrame,
    new_log_rows: list[dict],
) -> None:
    """物件DB・成約取消・変更ログをまとめて保存する。"""
    log_df = load_log(db_path)
    log_records = log_df.to_dict("records") if not log_df.empty else []
    log_records.extend(new_log_rows)
    log_full = (
        pd.DataFrame(log_records, columns=LOG_COLUMNS)
        if log_records
        else pd.DataFrame(columns=LOG_COLUMNS)
    )

    with pd.ExcelWriter(db_path, engine="openpyxl") as writer:
        db_df.to_excel(writer,      sheet_name=SHEET_DB,      index=False)
        archive_df.to_excel(writer, sheet_name=SHEET_REMOVED, index=False)
        log_full.to_excel(writer,   sheet_name=SHEET_LOG,     index=False)

    _apply_styles(db_path)
    logger.info(f"DB保存完了: {db_path} アクティブ{(db_df['状態']==STATUS_ACTIVE).sum() if not db_df.empty else 0}件 取消候補{(db_df['状態']==STATUS_CANDIDATE).sum() if not db_df.empty else 0}件")


# ----------------------------------------------------------------
# 内部ヘルパー
# ----------------------------------------------------------------

def _merge_conditions(existing: str, new_cond: str) -> str:
    parts = [s.strip() for s in str(existing).split(",") if s.strip()]
    if new_cond and new_cond not in parts:
        parts.append(new_cond)
    return ", ".join(parts)


def _log_row(now: str, cond: str, change: str, prop: dict, old_price: str = "") -> dict:
    return {
        "日時":      now,
        "検索条件名": cond,
        "変更":      change,
        "物件番号":   prop.get(ID_COL, ""),
        "所在地":     prop.get("所在地", ""),
        "価格":       prop.get(PRICE_COL, ""),
        "旧価格":     old_price,
    }


def _norm_price(s: str) -> str:
    return re.sub(r"[\s,、　円万]", "", str(s))


def _apply_styles(db_path: str) -> None:
    try:
        wb = openpyxl.load_workbook(db_path)
        if SHEET_DB not in wb.sheetnames:
            wb.save(db_path)
            return
        ws = wb[SHEET_DB]

        fill_candidate = PatternFill(start_color="FFE0E0", end_color="FFE0E0", fill_type="solid")
        fill_group     = PatternFill(start_color="DAE8FC", end_color="DAE8FC", fill_type="solid")

        for cell in ws[1]:
            cell.font      = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")

        # 列インデックス
        headers   = [c.value for c in ws[1]]
        status_i  = headers.index("状態")     + 1 if "状態"     in headers else None
        group_i   = headers.index("グループID") + 1 if "グループID" in headers else None

        for row in ws.iter_rows(min_row=2):
            status = row[status_i - 1].value if status_i else None
            grp    = row[group_i - 1].value if group_i else None
            if status == STATUS_CANDIDATE:
                for c in row:
                    c.fill = fill_candidate
            elif grp:
                for c in row:
                    c.fill = fill_group

        for col in ws.columns:
            w = max((len(str(c.value or "")) for c in col), default=0)
            ws.column_dimensions[col[0].column_letter].width = min(w + 2, 45)

        wb.save(db_path)
    except Exception as e:
        logger.warning(f"スタイル適用エラー（無視）: {e}")
