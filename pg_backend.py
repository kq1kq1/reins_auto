"""
Supabase (PostgreSQL) ストレージバックエンド

第1段階の位置づけ:
  スプレッドシートが「正」、こちらは「写し」。
  写しが安定して一致することを確認してから、正を移す。
  そのため書き込みに失敗しても本番を止めない（呼び出し側で握りつぶす）。

スプシとの決定的な違い:
  スプシは「全消し→全書き直し」だが、こちらは物件番号をキーにした
  UPSERT（あれば更新・なければ挿入）なので、通信断でデータが消えることがない。

必要ライブラリ:
  pip install "psycopg[binary]"
"""

import logging
import re
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_DEFAULT = "reins"
CHUNK = 500          # 1回のINSERTにまとめる行数（往復回数を減らすため）

STATUS_CLOSED = "成約・取消"

# DataFrameの日本語列 → DBの英語列
COLUMN_MAP = {
    "物件番号":     "property_no",
    "物件種別":     "property_type",
    "取引状況":     "deal_status",
    "取引態様":     "deal_type",
    "所在地":       "address",
    "建物名":       "building_name",
    "所在階":       "floor_info",
    "間取り":       "layout",
    "専有面積":     "exclusive_area",
    "建物面積":     "building_area",
    "土地面積":     "land_area",
    "価格":         "price",
    "㎡単価":       "unit_price_sqm",
    "坪単価":       "unit_price_tsubo",
    "管理費":       "management_fee",
    "用途地域":     "zoning",
    "建ぺい率":     "building_coverage",
    "容積率":       "floor_area_ratio",
    "接道状況":     "road_access",
    "接道１":       "road_1",
    "沿線駅":       "line_station",
    "交通":         "transport",
    "築年月":       "built_ym",
    "会社名":       "agency_name",
    "電話番号":     "agency_tel",
    "登録日":       "listed_on",
    "図面":         "drawing_path",
    "検出条件":     "source_conditions",
    "状態":         "status",
    "取消候補日":   "candidate_since",
    "未検出回数":   "miss_count",
    "グループID":   "group_id",
    "初回取得日":   "first_seen_on",
    "最終確認日":   "last_seen_on",
    "成約・取消日": "closed_on",
}

NUMERIC_COLS = {"price", "exclusive_area", "building_area", "land_area",
                "unit_price_sqm", "unit_price_tsubo", "management_fee"}
INT_COLS = {"miss_count"}
DATE_COLS = {"listed_on", "candidate_since", "first_seen_on",
             "last_seen_on", "closed_on"}

# DBへ書き込む列の順序（この順でINSERTする）
DB_COLUMNS = list(COLUMN_MAP.values()) + [
    "walk_min", "built_year", "identity_key", "raw",
]


# ----------------------------------------------------------------
# 値の変換
# ----------------------------------------------------------------

def _num(v):
    """3,480 のような表記を数値にする。「応談」や空文字は None。"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _date(v):
    """2026-08-07 や 2026/8/7 を date にする。読めなければ None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None


def _walk_min(transport):
    """徒歩7分 のような表記から 7 を取り出す。バス便など徒歩表記が無ければ None。"""
    if not transport:
        return None
    m = re.search(r"徒歩\s*(\d+)", str(transport))
    return int(m.group(1)) if m else None


def _built_year(built_ym):
    """2005年3月 から 2005 を取り出す。"""
    if not built_ym:
        return None
    m = re.search(r"(?:19|20)\d{2}", str(built_ym))
    return int(m.group()) if m else None


def _identity_str(rec):
    """processor の identity キー（タプル）を1本の文字列にする。
    物件番号が変わっても同一物件を追えるようにするため。"""
    from processor import _identity_key      # 循環importを避けて遅延読み込み
    k = _identity_key(rec)
    if not k:
        return None
    return "|".join("" if x is None else str(x) for x in k)


# ----------------------------------------------------------------
# 接続
# ----------------------------------------------------------------

def is_enabled(cfg):
    return bool((cfg or {}).get("enabled")) and bool((cfg or {}).get("dsn"))


def connect(cfg):
    import psycopg
    return psycopg.connect(cfg["dsn"], connect_timeout=cfg.get("timeout", 20))


# ----------------------------------------------------------------
# 行の組み立て
# ----------------------------------------------------------------

def _row(rec, force_status=None):
    """1物件のdictを、DB_COLUMNS の順に並べたリストにする。

    force_status を渡すと「状態」列を無条件で上書きする。
    成約・取消シートの行は状態列に古い値（取消候補など）が残っているため、
    どのシートから来たかで状態を決める必要がある。
    """
    from psycopg.types.json import Jsonb

    out = []
    for jp, en in COLUMN_MAP.items():
        v = rec.get(jp, "")
        if v is None or (isinstance(v, float) and pd.isna(v)):
            v = ""
        if en == "status":
            v = force_status or str(v).strip()
        elif en in NUMERIC_COLS:
            v = _num(v)
        elif en in INT_COLS:
            v = _int(v) or 0
        elif en in DATE_COLS:
            v = _date(v)
        else:
            v = str(v).strip() or None
        out.append(v)

    out.append(_walk_min(rec.get("交通", "")))
    out.append(_built_year(rec.get("築年月", "")))
    out.append(_identity_str(rec))
    # 原文はすべて raw に残す（数値化に失敗しても情報を失わないため）
    out.append(Jsonb({k: ("" if v is None else str(v)) for k, v in rec.items()}))
    return out


def build_rows(db_df, archive_df):
    """物件DBと成約・取消を合わせて、DB投入用の行リストにする。

    同じ物件番号が両方に居た場合は物件DB側を優先する
    （UPSERTは1文の中で同じキーを2回扱えないため、必ず1行に絞る）。
    戻り値: (行リスト, 重複で除外した件数)
    """
    rows, seen, dup = [], set(), 0

    # 成約・取消シートから来た行は、状態列の値によらず「成約・取消」で確定させる
    for df, force_status in ((db_df, None), (archive_df, STATUS_CLOSED)):
        if df is None or df.empty:
            continue
        for rec in df.to_dict("records"):
            pid = str(rec.get("物件番号", "")).strip()
            if not pid:
                continue
            if pid in seen:
                dup += 1
                continue
            seen.add(pid)
            rows.append(_row(rec, force_status))
    return rows, dup


# ----------------------------------------------------------------
# 書き込み
# ----------------------------------------------------------------

def upsert_properties(cfg, db_df, archive_df):
    """物件を一括UPSERT（あれば更新・なければ挿入）。戻り値は書き込んだ行数。

    スプシのように全消ししないので、途中で失敗しても既存データは残る。
    """
    schema = cfg.get("schema", SCHEMA_DEFAULT)
    rows, dup = build_rows(db_df, archive_df)
    if dup:
        logger.warning(f"物件番号の重複を{dup}件スキップしました（物件DB側を優先）")
    if not rows:
        return 0

    cols = ", ".join(DB_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in DB_COLUMNS if c != "property_no")
    one = "(" + ", ".join(["%s"] * len(DB_COLUMNS)) + ")"

    with connect(cfg) as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                stmt = (
                    f"insert into {schema}.properties ({cols}) values "
                    + ", ".join([one] * len(chunk))
                    + f" on conflict (property_no) do update set {updates}, updated_at = now()"
                )
                cur.execute(stmt, [v for r in chunk for v in r])
        conn.commit()
    return len(rows)


def append_logs(cfg, log_rows):
    """変更ログを追記する。スプシと違い年で分ける必要がない。"""
    if not log_rows:
        return 0
    schema = cfg.get("schema", SCHEMA_DEFAULT)

    vals = []
    for r in log_rows:
        ts = str(r.get("日時", "")).strip()
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        except ValueError:
            dt = datetime.now()
        vals.append([
            dt,
            (r.get("検索条件名") or None),
            (r.get("変更") or None),
            (r.get("物件番号") or None),
            (r.get("所在地") or None),
            (str(r.get("価格", "")).strip() or None),
            (str(r.get("旧価格", "")).strip() or None),
        ])

    one = "(" + ", ".join(["%s"] * 7) + ")"
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            for i in range(0, len(vals), CHUNK):
                chunk = vals[i:i + CHUNK]
                stmt = (
                    f"insert into {schema}.change_log "
                    f"(logged_at, condition_name, change_type, property_no, address, price, old_price) "
                    f"values " + ", ".join([one] * len(chunk))
                )
                cur.execute(stmt, [v for r in chunk for v in r])
        conn.commit()
    return len(vals)


def record_run(cfg, mode, **counts):
    """実行1回分を記録する。「今週の取消候補は多いのか」を後から集計できるようにする。"""
    schema = cfg.get("schema", SCHEMA_DEFAULT)
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"insert into {schema}.runs "
                f"(mode, scraped_count, new_count, price_changed_count, "
                f"candidate_count, closed_count) values (%s, %s, %s, %s, %s, %s)",
                (mode, counts.get("scraped"), counts.get("new"),
                 counts.get("price_changed"), counts.get("candidate"),
                 counts.get("closed")),
            )
        conn.commit()


# ----------------------------------------------------------------
# 確認用
# ----------------------------------------------------------------

def counts(cfg):
    """状態ごとの件数を返す（スプシとの突合に使う）。"""
    schema = cfg.get("schema", SCHEMA_DEFAULT)
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select status, count(*) from {schema}.properties group by status")
            by_status = dict(cur.fetchall())
            cur.execute(f"select count(*) from {schema}.properties")
            total = cur.fetchone()[0]
            cur.execute(f"select count(*) from {schema}.change_log")
            logs = cur.fetchone()[0]
    return {"total": total, "by_status": by_status, "change_log": logs}
