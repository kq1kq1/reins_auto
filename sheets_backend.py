"""
Google Sheets ストレージバックエンド

processor.py の I/O層（load_db / load_archive / load_log / save_db /
load_state / save_state）から呼ばれ、Excelの代わりにGoogleスプレッドシートを
読み書きする。差分検出などの核心ロジックには一切関与しない。

認証: サービスアカウント（JSONキー）。スプレッドシートをそのサービスアカウントの
メールアドレスに「編集者」で共有しておくこと。

必要ライブラリ:
  pip install gspread google-auth
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SHEET_STATE = "実行状態"

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 接続は使い回す（1実行で複数回読み書きするため）
_spreadsheet = None


def _connect(cfg: dict):
    """スプレッドシートに接続して返す（キャッシュあり）。

    認証方式は config の storage.auth で切替:
      "service_account"（既定）: サービスアカウントJSONで認証（無人向き）
      "oauth"                  : 自分のGoogleアカウントでブラウザ認証（初回のみ）
    """
    global _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet

    import gspread

    sid  = cfg.get("spreadsheet_id", "")
    if not sid:
        raise ValueError("config.json の storage に spreadsheet_id を設定してください")

    auth = cfg.get("auth", "service_account")

    if auth == "oauth":
        # 自分のGoogleアカウントでOAuth認証（初回はブラウザが開く）
        client_path = cfg.get("oauth_client_json", "oauth_client.json")
        token_path  = cfg.get("oauth_token_json", "authorized_user.json")
        if not Path(client_path).exists():
            raise ValueError(
                f"OAuthクライアントJSONが見つかりません: {client_path}\n"
                "Google Cloud Console でOAuthクライアントID（デスクトップ）を作成し、"
                "ダウンロードしたJSONをこのパスに置いてください"
            )
        client = gspread.oauth(
            credentials_filename=str(client_path),
            authorized_user_filename=str(token_path),
            scopes=_SCOPES,
        )
    else:
        from google.oauth2.service_account import Credentials
        sa_path = cfg.get("service_account_json", "")
        if not sa_path:
            raise ValueError(
                "config.json の storage に service_account_json を設定してください"
            )
        creds = Credentials.from_service_account_file(sa_path, scopes=_SCOPES)
        client = gspread.authorize(creds)

    _spreadsheet = client.open_by_key(sid)
    return _spreadsheet


def _get_ws(ss, title: str):
    """ワークシートを取得。無ければ None。"""
    import gspread
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return None


def _get_or_create_ws(ss, title: str, ncols: int):
    """ワークシートを取得。無ければ作成する。"""
    ws = _get_ws(ss, title)
    if ws is None:
        ws = ss.add_worksheet(title=title, rows=1, cols=max(ncols, 1))
    return ws


# ----------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------

def read_sheet(cfg: dict, title: str, columns: list[str]) -> pd.DataFrame:
    """指定シートを DataFrame として読み込む。無ければ空DF。"""
    try:
        ss = _connect(cfg)
        ws = _get_ws(ss, title)
        if ws is None:
            return pd.DataFrame(columns=columns)
        values = ws.get_all_values()
        if not values or len(values) < 1:
            return pd.DataFrame(columns=columns)
        header = values[0]
        rows   = values[1:]
        df = pd.DataFrame(rows, columns=header).fillna("")
        # 期待するカラムが無ければ空で補完
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        return df[columns]
    except Exception as e:
        logger.error(f"Sheets読込エラー [{title}]: {e}")
        return pd.DataFrame(columns=columns)


# ----------------------------------------------------------------
# 書き込み
# ----------------------------------------------------------------

def _write_ws(ss, title: str, df: pd.DataFrame, columns: list[str]) -> None:
    """1シートを丸ごと上書きする（ヘッダー込み）。"""
    ws = _get_or_create_ws(ss, title, len(columns))

    # DataFrame を文字列2次元配列に（欠損は空文字）
    if df is None or df.empty:
        body = []
    else:
        d = df.reindex(columns=columns).fillna("").astype(str)
        body = d.values.tolist()

    values = [columns] + body

    # グリッドサイズを合わせてから書き込む（範囲外エラー防止）
    nrows = max(len(values), 1)
    ncols = max(len(columns), 1)
    try:
        ws.resize(rows=nrows, cols=ncols)
    except Exception:
        pass

    # 既存内容をクリアして一括更新
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="RAW")

    # ヘッダーを太字＋1行目固定（初回のみでも毎回でも軽い）
    try:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def write_db_archive(
    cfg: dict,
    db_df: pd.DataFrame,
    archive_df: pd.DataFrame,
    db_cols: list[str],
    removed_cols: list[str],
) -> None:
    """物件DB・成約取消の2シートを丸ごと上書きする（ログは別途 append_logs で追記）。"""
    ss = _connect(cfg)
    _write_ws(ss, "物件DB",   db_df,      db_cols)
    _write_ws(ss, "成約・取消", archive_df, removed_cols)


def append_logs(cfg: dict, rows_by_year: dict, log_cols: list[str]) -> None:
    """変更ログを年別シート（変更ログ_YYYY）に末尾追記する。
    全体を読み書きせず append するので行数が増えても高速。
    rows_by_year: {"2026": [[...], ...]} ヘッダー無しの値行リスト。
    """
    if not rows_by_year:
        return
    ss = _connect(cfg)
    for year, rows in rows_by_year.items():
        if not rows:
            continue
        title = f"変更ログ_{year}"
        ws = _get_ws(ss, title)
        if ws is None:
            ws = ss.add_worksheet(title=title, rows=len(rows) + 1, cols=len(log_cols))
            ws.update(range_name="A1", values=[log_cols], value_input_option="RAW")
            try:
                ws.freeze(rows=1)
                ws.format("1:1", {"textFormat": {"bold": True}})
            except Exception:
                pass
        ws.append_rows(rows, value_input_option="RAW")


def write_sheet(cfg: dict, title: str, df: pd.DataFrame, columns: list[str]) -> None:
    """任意シートを丸ごと上書き（マイグレーション用）。"""
    ss = _connect(cfg)
    _write_ws(ss, title, df, columns)


def delete_sheet(cfg: dict, title: str) -> None:
    """指定シートを削除（存在すれば）。マイグレーション用。"""
    ss = _connect(cfg)
    ws = _get_ws(ss, title)
    if ws is not None:
        try:
            ss.del_worksheet(ws)
        except Exception as e:
            logger.warning(f"シート削除失敗 [{title}]: {e}")


def list_log_year_sheets(cfg: dict) -> list[str]:
    """変更ログ_YYYY 形式のシート名一覧を返す。"""
    ss = _connect(cfg)
    names = [ws.title for ws in ss.worksheets()]
    return sorted(n for n in names if n.startswith("変更ログ_"))


# ----------------------------------------------------------------
# 実行状態（前回実行日など）を専用シートに key-value で保存
# ----------------------------------------------------------------

def read_state(cfg: dict) -> dict:
    try:
        ss = _connect(cfg)
        ws = _get_ws(ss, SHEET_STATE)
        if ws is None:
            return {}
        values = ws.get_all_values()
        state = {}
        for row in values:
            if len(row) >= 2 and row[0]:
                state[row[0]] = row[1]
        return state
    except Exception as e:
        logger.warning(f"Sheets state読込失敗（空扱い）: {e}")
        return {}


def write_state(cfg: dict, state: dict) -> None:
    try:
        ss = _connect(cfg)
        ws = _get_or_create_ws(ss, SHEET_STATE, 2)
        values = [[str(k), str(v)] for k, v in state.items()]
        if not values:
            values = [["", ""]]
        ws.resize(rows=max(len(values), 1), cols=2)
        ws.clear()
        ws.update(range_name="A1", values=values, value_input_option="RAW")
    except Exception as e:
        logger.warning(f"Sheets state保存失敗: {e}")


# ----------------------------------------------------------------
# チーム共通設定（設定シート / 検索条件シート）
# ----------------------------------------------------------------

SHEET_SETTINGS   = "設定"
SHEET_CONDITIONS = "検索条件"


def read_settings(cfg: dict) -> dict:
    """「設定」シートを key-value の dict で返す。無ければ空。"""
    try:
        ss = _connect(cfg)
        ws = _get_ws(ss, SHEET_SETTINGS)
        if ws is None:
            return {}
        out = {}
        for row in ws.get_all_values():
            if len(row) >= 2 and str(row[0]).strip():
                out[str(row[0]).strip()] = str(row[1]).strip()
        return out
    except Exception as e:
        logger.warning(f"設定シート読込失敗（空扱い）: {e}")
        return {}


def write_settings(cfg: dict, settings: dict) -> None:
    """「設定」シートに key-value を書き込む（ヘッダー付き）。"""
    ss = _connect(cfg)
    ws = _get_or_create_ws(ss, SHEET_SETTINGS, 2)
    values = [["キー", "値"]] + [[str(k), str(v)] for k, v in settings.items()]
    ws.resize(rows=max(len(values), 1), cols=2)
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="RAW")
    try:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def read_conditions(cfg: dict) -> tuple[list, list]:
    """「検索条件」シートから (daily, weekly) の条件リストを返す。
    シート形式: 列 [区分, id, name]。区分は 'daily' / 'weekly'。
    """
    try:
        ss = _connect(cfg)
        ws = _get_ws(ss, SHEET_CONDITIONS)
        if ws is None:
            return [], []
        daily, weekly = [], []
        rows = ws.get_all_values()
        for row in rows[1:]:  # ヘッダー行を飛ばす
            if len(row) < 2:
                continue
            kbn = str(row[0]).strip().lower()
            cid = str(row[1]).strip()
            name = str(row[2]).strip() if len(row) >= 3 else ""
            if not cid:
                continue
            item = {"id": int(cid) if cid.isdigit() else cid, "name": name}
            if kbn == "weekly":
                weekly.append(item)
            elif kbn == "daily":
                daily.append(item)
        return daily, weekly
    except Exception as e:
        logger.warning(f"検索条件シート読込失敗（空扱い）: {e}")
        return [], []


def write_conditions(cfg: dict, daily: list, weekly: list) -> None:
    """「検索条件」シートに daily/weekly の条件を書き込む。"""
    ss = _connect(cfg)
    ws = _get_or_create_ws(ss, SHEET_CONDITIONS, 3)
    values = [["区分", "id", "name"]]
    for c in (daily or []):
        values.append(["daily", str(c.get("id", "")), str(c.get("name", ""))])
    for c in (weekly or []):
        values.append(["weekly", str(c.get("id", "")), str(c.get("name", ""))])
    ws.resize(rows=max(len(values), 1), cols=3)
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="RAW")
    try:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass
