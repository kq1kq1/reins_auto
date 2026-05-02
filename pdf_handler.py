"""
PDF結合・印刷・自動削除モジュール

印刷方法:
  - SumatraPDF が見つかれば使う（B4・白黒などのオプション指定可能）
  - 見つからなければ os.startfile で default プリンタへ送信
"""

import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_exports(export_dir: str, days: int = 7) -> int:
    """N日以上前のファイルを削除する。削除件数を返す。"""
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    for f in Path(export_dir).glob("*"):
        if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                logger.warning(f"削除失敗: {f.name}: {e}")
    if deleted:
        logger.info(f"古いファイルを削除: {deleted}件（{days}日以上前）")
    return deleted


def merge_pdfs(pdf_paths: list[Path], out_dir: Path) -> Path | None:
    """複数PDFを1つに結合する。失敗したらNoneを返す。"""
    if not pdf_paths:
        return None
    try:
        from pypdf import PdfWriter
        writer = PdfWriter()
        for p in pdf_paths:
            writer.append(str(p))
        out = out_dir / f"新規物件_{datetime.now():%Y%m%d_%H%M}.pdf"
        with open(out, "wb") as f:
            writer.write(f)
        logger.info(f"PDF結合完了: {out.name} ({len(pdf_paths)}件)")
        return out
    except ImportError:
        logger.error("pypdf が未インストールです: pip install pypdf")
        return None
    except Exception as e:
        logger.error(f"PDF結合エラー: {e}")
        return None


def print_pdf(pdf_path: Path, print_cfg: dict | None = None) -> bool:
    """
    PDFを印刷する。
    print_cfg:
      paper_size:   "B4" / "A4" など（SumatraPDF使用時のみ有効）
      color_mode:   "monochrome" / "color"  （SumatraPDF使用時のみ）
      printer_name: 空ならデフォルトプリンタ
      sumatra_path: SumatraPDFのexeパス。空なら自動検出。
    """
    print_cfg = print_cfg or {}
    sumatra = _find_sumatra(print_cfg.get("sumatra_path", ""))

    if sumatra:
        return _print_with_sumatra(sumatra, pdf_path, print_cfg)
    return _print_with_default(pdf_path, print_cfg.get("printer_name", ""))


def _find_sumatra(custom_path: str) -> str | None:
    """SumatraPDFのexeを探す。見つからなければNone。"""
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    candidates += [
        os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"),
        r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return None


def _print_with_sumatra(sumatra: str, pdf_path: Path, cfg: dict) -> bool:
    """SumatraPDFで印刷設定を指定して印刷する。"""
    settings_parts = []
    paper = cfg.get("paper_size", "")
    if paper:
        settings_parts.append(f"paper={paper}")
    color = cfg.get("color_mode", "")
    if color == "monochrome":
        settings_parts.append("monochrome")
    elif color == "color":
        settings_parts.append("color")
    settings_parts.append("fit")
    settings = ",".join(settings_parts)

    printer = cfg.get("printer_name", "")
    if printer:
        cmd = [sumatra, "-print-to", printer, "-print-settings", settings,
               "-silent", "-exit-when-done", str(pdf_path)]
    else:
        cmd = [sumatra, "-print-to-default", "-print-settings", settings,
               "-silent", "-exit-when-done", str(pdf_path)]

    try:
        subprocess.Popen(cmd)
        logger.info(f"SumatraPDFで印刷送信: {pdf_path.name} (設定: {settings})")
        return True
    except Exception as e:
        logger.error(f"SumatraPDF印刷エラー: {e}")
        return False


def _print_with_default(pdf_path: Path, printer_name: str = "") -> bool:
    """os.startfileでデフォルトプリンタへ送る（プリンタ設定はWindows側依存）。"""
    try:
        if printer_name:
            os.startfile(str(pdf_path), "printto", printer_name)
        else:
            os.startfile(str(pdf_path), "print")
        logger.info(f"印刷送信完了: {pdf_path.name}（デフォルト設定）")
        return True
    except Exception as e:
        logger.error(f"印刷エラー: {e}")
        return False
