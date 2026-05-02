"""
メール通知モジュール

GmailのSMTPを使う場合:
  - Googleアカウント → セキュリティ → 「アプリパスワード」を発行して smtp_password に設定
  - 通常のパスワードは使えません（2段階認証が必要）
"""

import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)


def send_email(cfg: dict, subject: str, body_html: str) -> bool:
    email_from    = cfg.get("email_from", "")
    email_to      = cfg.get("email_to", "")
    smtp_server   = cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port     = cfg.get("smtp_port", 587)
    smtp_password = cfg.get("smtp_password", "")

    if not all([email_from, email_to, smtp_password]):
        logger.warning("メール設定が不完全です（config.jsonの notification セクションを確認）")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = email_from
    msg["To"]      = email_to
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(email_from, smtp_password)
            server.sendmail(email_from, email_to, msg.as_string())
        logger.info(f"メール送信成功: {subject}")
        return True
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")
        return False


# ----------------------------------------------------------------
# メール本文生成
# ----------------------------------------------------------------

def build_summary_email(diff: dict, total: int, mode: str = "daily") -> tuple[str, str]:
    now = datetime.now()
    n_new       = len(diff.get("new", []))
    n_changed   = len(diff.get("price_changed", []))
    n_cand      = len(diff.get("candidates", []))
    n_confirmed = len(diff.get("confirmed", []))
    n_restored  = len(diff.get("restored", []))

    mode_label = "週次" if mode in ("weekly", "auto_weekly") else "日次"

    parts = [f"新規{n_new}件", f"価格変更{n_changed}件"]
    if n_cand:
        parts.append(f"取消候補{n_cand}件")
    if n_confirmed:
        parts.append(f"取消確定{n_confirmed}件")

    subject = f"[REINS] {now:%m/%d} {mode_label} " + " / ".join(parts)

    sections = []
    if diff.get("new"):
        sections.append(_section("🆕 新規物件", diff["new"], color="#1a7340", border="#1a7340"))
    if diff.get("price_changed"):
        sections.append(_section("💰 価格変更", diff["price_changed"], color="#7a4f00", border="#e6ac00", show_old_price=True))
    if diff.get("candidates"):
        sections.append(_section("⚠️ 取消の可能性あり（要確認）", diff["candidates"], color="#7a4f00", border="#ff9933",
                                 note="REINSから消えた物件です。電話等で確認し、まだあれば「戻す.bat」または Excel の状態カラムを「アクティブ」に書き換えてください。猶予期間内に戻さないと自動で成約・取消扱いになります。"))
    if diff.get("confirmed"):
        sections.append(_section("🔴 成約・取消確定", diff["confirmed"], color="#8b0000", border="#cc0000",
                                 note="猶予期間内に確認操作がなかったため、成約・取消シートに移動しました。"))
    if diff.get("restored"):
        sections.append(_section("🔄 取消候補から復活", diff["restored"], color="#1c4587", border="#1c4587",
                                 note="取消候補だった物件が再度REINSに出てきたため、アクティブに戻りました。"))

    if not sections:
        sections.append("<p style='color:#555'>本日の変化はありませんでした。</p>")

    subtitle = (
        f"アクティブ物件: {total}件　|　"
        f"新規: {n_new}　価格変更: {n_changed}　"
        f"取消候補: {n_cand}　取消確定: {n_confirmed}　復活: {n_restored}"
    )
    body = _html_wrap(
        title=f"REINS物件情報 {now:%Y年%m月%d日} ({mode_label})",
        subtitle=subtitle,
        content="\n".join(sections),
    )
    return subject, body


def _section(
    title: str, props: list[dict], color: str, border: str,
    show_old_price: bool = False, note: str = "",
) -> str:
    rows = "".join(_prop_row(p, show_old_price=show_old_price) for p in props)
    note_html = f"<p style='font-size:12px;color:#555;margin:4px 0 8px'>{note}</p>" if note else ""
    return f"""
    <h2 style="color:{color};border-left:4px solid {border};padding-left:10px">
      {title} {len(props)}件
    </h2>
    {note_html}
    <table {_TABLE_STYLE}>
      {_table_header(show_old_price=show_old_price)}
      {rows}
    </table>"""


_TABLE_STYLE = 'style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:24px"'
_TH = 'style="background:#f0f0f0;border:1px solid #ccc;padding:6px 10px;text-align:left"'
_TD = 'style="border:1px solid #ddd;padding:6px 10px;vertical-align:top"'
_TD_NUM = 'style="border:1px solid #ddd;padding:6px 10px;text-align:right;white-space:nowrap"'


def _table_header(show_old_price: bool = False) -> str:
    old_col = f"<th {_TH}>変更前価格</th>" if show_old_price else ""
    return f"""
    <tr>
      <th {_TH}>物件番号</th>
      <th {_TH}>会社名</th>
      <th {_TH}>種別</th>
      <th {_TH}>所在地</th>
      <th {_TH}>交通</th>
      <th {_TH}>価格（万円）</th>
      {old_col}
      <th {_TH}>間取り</th>
      <th {_TH}>面積(㎡)</th>
      <th {_TH}>築年月</th>
      <th {_TH}>登録日</th>
    </tr>"""


def _prop_row(p: dict, show_old_price: bool = False) -> str:
    old_col = ""
    if show_old_price:
        old = p.get("旧価格", "")
        new = p.get("価格", "")
        arrow = _price_arrow(old, new)
        old_col = f"<td {_TD_NUM}>{old}</td>"
        price_cell = f"<td {_TD_NUM}><b>{new}</b> {arrow}</td>"
    else:
        price_cell = f"<td {_TD_NUM}>{p.get('価格', '')}</td>"

    area = p.get("専有面積", "") or p.get("土地面積", "")
    grp  = p.get("グループID", "")
    grp_label = f' <span style="color:#1c4587;font-size:11px">[{grp}]</span>' if grp else ""

    return f"""
    <tr>
      <td {_TD}>{p.get("物件番号", "")}{grp_label}</td>
      <td {_TD}>{p.get("会社名", "")}</td>
      <td {_TD}>{p.get("物件種別", "")}</td>
      <td {_TD}>{p.get("所在地", "")}</td>
      <td {_TD}>{p.get("交通", "")}</td>
      {price_cell}
      {old_col}
      <td {_TD}>{p.get("間取り", "")}</td>
      <td {_TD_NUM}>{area}</td>
      <td {_TD}>{p.get("築年月", "")}</td>
      <td {_TD}>{p.get("登録日", "")}</td>
    </tr>"""


def _price_arrow(old: str, new: str) -> str:
    try:
        o = float(str(old).replace(",", "").replace("万", "").strip())
        n = float(str(new).replace(",", "").replace("万", "").strip())
        if n < o:
            return f'<span style="color:green">▼{o-n:.0f}万</span>'
        elif n > o:
            return f'<span style="color:red">▲{n-o:.0f}万</span>'
    except Exception:
        pass
    return ""


def _html_wrap(title: str, subtitle: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8">
<style>
  body {{ font-family: "Meiryo","Hiragino Sans",sans-serif; color:#222; max-width:900px; margin:0 auto; padding:20px; }}
  .header {{ background:#1c4587; color:white; padding:16px 20px; border-radius:6px; margin-bottom:20px; }}
  .header h1 {{ margin:0; font-size:18px; }}
  .header p  {{ margin:6px 0 0; font-size:13px; opacity:0.85; }}
  table tr:nth-child(even) {{ background:#f9f9f9; }}
  .footer {{ font-size:11px; color:#999; margin-top:30px; border-top:1px solid #eee; padding-top:10px; }}
</style>
</head>
<body>
<div class="header">
  <h1>{title}</h1>
  <p>{subtitle}</p>
</div>
{content}
<div class="footer">このメールはREINS物件モニタリングシステムにより自動送信されました。</div>
</body>
</html>"""
