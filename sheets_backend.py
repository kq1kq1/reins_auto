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
import socket
import time
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


class SheetsReadError(Exception):
    """スプレッドシートの読み込みに失敗した（通信断など）。

    「読めなかった」を空データとして返すと、呼び出し側が『0件のDB』と
    誤認して全件を新規扱い→シート全消し上書き、という事故になる。
    区別できるように専用の例外を投げる。
    """


class SheetsWriteError(Exception):
    """スプレッドシートの書き込みに失敗した。"""


# 通信の瞬断はリトライで大半が救えるので、間隔を空けて数回試す
_RETRY_WAITS = (5, 15, 30)


def _is_network_error(e: Exception) -> bool:
    """時間をおけば直る可能性のあるエラーか（DNS断・接続断・タイムアウト・Google側障害）。

    認証エラーや権限エラーはリトライしても無駄なので False を返す。
    """
    try:
        import requests
        if isinstance(e, (requests.exceptions.ConnectionError,
                          requests.exceptions.Timeout)):
            return True
    except Exception:
        pass

    if isinstance(e, (socket.gaierror, socket.timeout, ConnectionError, TimeoutError)):
        return True

    # 接続を張り直すときはアクセストークンも取り直すため、通信断は認証層側で
    # TransportError として出てくる（requests の ConnectionError にはならない）。
    # これを拾わないと2回目以降のリトライが効かない。
    # ※ RefreshError は鍵が無効なケースも含むのでリトライしない。
    try:
        from google.auth.exceptions import TransportError
        if isinstance(e, TransportError):
            return True
    except Exception:
        pass

    # gspread のAPIエラーは 429(レート制限) と 5xx(Google側障害) だけリトライ対象
    try:
        import gspread
        if isinstance(e, gspread.exceptions.APIError):
            code = getattr(getattr(e, "response", None), "status_code", 0)
            return code == 429 or 500 <= int(code or 0) < 600
    except Exception:
        pass

    return False


def _retry(what: str, fn, *args, **kwargs):
    """通信エラーなら待って再試行する。最後まで失敗したら例外をそのまま投げる。"""
    global _spreadsheet

    last: Exception | None = None
    for i, wait in enumerate((0,) + _RETRY_WAITS):
        if wait:
            msg = f"{what}: 通信エラー。{wait}秒後に再試行します（{i}/{len(_RETRY_WAITS)}）"
            logger.warning(msg)
            print(f"  ⏳ {msg}")
            time.sleep(wait)
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_network_error(e):
                raise
            last = e
            # 切断後のセッションを引きずらないよう、接続キャッシュを捨てて張り直す
            _spreadsheet = None
    raise last


def check_connection(cfg: dict) -> None:
    """起動直後の疎通確認。つながらなければ SheetsReadError。

    30分かけて巡回した後で「実は最初から圏外でした」となるのを防ぐため、
    処理を始める前に一度だけ確認する。
    """
    try:
        _retry("スプシ疎通確認", _connect, cfg)
    except Exception as e:
        raise SheetsReadError(f"スプレッドシートに接続できません: {e}") from e


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

def read_sheet(cfg: dict, title: str, columns: list[str],
               required: bool = False) -> pd.DataFrame:
    """指定シートを DataFrame として読み込む。シート自体が無ければ空DF。

    required=True のときは、通信エラー等で読めなかった場合に SheetsReadError を投げる。
    物件DBなど「空で続行すると全消しにつながる」シートは必ず required=True で読むこと。
    """
    def _read():
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

    try:
        return _retry(f"Sheets読込[{title}]", _read)
    except Exception as e:
        logger.error(f"Sheets読込エラー [{title}]: {e}")
        if required:
            raise SheetsReadError(f"シート「{title}」の読み込みに失敗しました: {e}") from e
        return pd.DataFrame(columns=columns)


# ----------------------------------------------------------------
# 書き込み
# ----------------------------------------------------------------

def _write_ws(ss, title: str, df: pd.DataFrame, columns: list[str]) -> None:
    """1シートを丸ごと上書きする（ヘッダー込み）。

    「先に clear してから書く」順序だと、その隙間で通信が切れたときに
    シートが空のまま残ってしまう。そのため
      ① 足りなければ広げる（非破壊） → ② 全セル上書き → ③ 余りを切り詰める
    の順にして、書き込みが成功するまで既存データを消さない。
    """
    ws = _get_or_create_ws(ss, title, len(columns))

    # DataFrame を文字列2次元配列に（欠損は空文字）
    if df is None or df.empty:
        body = []
    else:
        d = df.reindex(columns=columns).fillna("").astype(str)
        body = d.values.tolist()

    values = [columns] + body
    nrows = max(len(values), 1)
    ncols = max(len(columns), 1)

    # ① 書き込みに必要なサイズが足りなければ先に広げる（広げるだけなので既存データは無傷）
    cur_rows = int(getattr(ws, "row_count", 0) or 0)
    cur_cols = int(getattr(ws, "col_count", 0) or 0)
    if cur_rows < nrows or cur_cols < ncols:
        _retry(f"Sheets拡張[{title}]", ws.resize,
               rows=max(cur_rows, nrows), cols=max(cur_cols, ncols))

    # ② 全セルを上書き（clearしないので、ここで失敗しても旧データは残る）
    _retry(f"Sheets書込[{title}]", ws.update,
           range_name="A1", values=values, value_input_option="RAW")

    # ③ 書き込み成功後に、余った行・列を切り詰める（古い残骸の読み戻し防止）
    cur_rows = int(getattr(ws, "row_count", 0) or 0)
    cur_cols = int(getattr(ws, "col_count", 0) or 0)
    if cur_rows > nrows or cur_cols > ncols:
        try:
            _retry(f"Sheets縮小[{title}]", ws.resize, rows=nrows, cols=ncols)
        except Exception as e:
            # データ本体は書けているので処理は続行する。ただし古い行が下に残るので警告する。
            logger.error(f"シート「{title}」の余分な行の削除に失敗: {e}")
            print(f"  ⚠️ シート「{title}」の{nrows + 1}行目以降に古いデータが残っている可能性があります。"
                  f"手動で削除するか、通信回復後にもう一度実行してください。")

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
    ss = _retry("Sheets接続", _connect, cfg)
    _write_ws(ss, "物件DB",   db_df,      db_cols)
    _write_ws(ss, "成約・取消", archive_df, removed_cols)


def append_logs(cfg: dict, rows_by_year: dict, log_cols: list[str]) -> None:
    """変更ログを年別シート（変更ログ_YYYY）に末尾追記する。
    全体を読み書きせず append するので行数が増えても高速。
    rows_by_year: {"2026": [[...], ...]} ヘッダー無しの値行リスト。
    """
    if not rows_by_year:
        return
    ss = _retry("Sheets接続", _connect, cfg)
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
        _retry(f"Sheetsログ追記[{title}]", ws.append_rows, rows, value_input_option="RAW")


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
    def _read():
        ss = _retry("Sheets接続", _connect, cfg)
        ws = _get_ws(ss, SHEET_STATE)
        if ws is None:
            return {}
        values = ws.get_all_values()
        state = {}
        for row in values:
            if len(row) >= 2 and row[0]:
                state[row[0]] = row[1]
        return state

    try:
        return _retry("Sheets state読込", _read)
    except Exception as e:
        # 空扱いにすると前回実行日が失われ、検索範囲が前日だけに狭まる（＝取りこぼす）。
        # 致命的ではないので続行するが、気づけるように ERROR で残す。
        logger.error(f"Sheets state読込失敗（前回実行日が使えません）: {e}")
        print("  ⚠️ 前回実行日が取得できませんでした。検索範囲が前日〜今日に狭まります。")
        return {}


def write_state(cfg: dict, state: dict) -> None:
    def _write():
        ss = _retry("Sheets接続", _connect, cfg)
        ws = _get_or_create_ws(ss, SHEET_STATE, 2)
        values = [[str(k), str(v)] for k, v in state.items()]
        if not values:
            values = [["", ""]]
        # 物件DBと同じく「広げる→上書き→縮める」順（途中で切れても空にしない）
        cur_rows = int(getattr(ws, "row_count", 0) or 0)
        if cur_rows < len(values):
            ws.resize(rows=len(values), cols=2)
        ws.update(range_name="A1", values=values, value_input_option="RAW")
        if int(getattr(ws, "row_count", 0) or 0) > len(values):
            ws.resize(rows=max(len(values), 1), cols=2)

    try:
        _retry("Sheets state保存", _write)
    except Exception as e:
        logger.error(f"Sheets state保存失敗（次回の検索範囲が前日からになります）: {e}")
        print("  ⚠️ 実行状態の保存に失敗しました。次回は前日〜今日の範囲で検索されます。")


# ----------------------------------------------------------------
# チーム共通設定（設定シート / 検索条件シート）
# ----------------------------------------------------------------

SHEET_SETTINGS   = "設定"
SHEET_CONDITIONS = "検索条件"


def read_settings(cfg: dict) -> dict:
    """「設定」シートを key-value の dict で返す。無ければ空。"""
    def _read():
        ss = _retry("Sheets接続", _connect, cfg)
        ws = _get_ws(ss, SHEET_SETTINGS)
        if ws is None:
            return {}
        out = {}
        for row in ws.get_all_values():
            if len(row) >= 2 and str(row[0]).strip():
                out[str(row[0]).strip()] = str(row[1]).strip()
        return out

    try:
        return _retry("設定シート読込", _read)
    except Exception as e:
        logger.error(f"設定シート読込失敗（ローカル設定で続行）: {e}")
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
    def _read():
        ss = _retry("Sheets接続", _connect, cfg)
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

    try:
        return _retry("検索条件シート読込", _read)
    except Exception as e:
        logger.error(f"検索条件シート読込失敗（ローカル設定で続行）: {e}")
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
