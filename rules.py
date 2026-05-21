"""
重複除外・グルーピングルール

ルールの追加方法:
  apply_rules() の末尾に新しい関数呼び出しを追加する。
  各関数は list[dict] を受け取り list[dict] を返す。
"""

import re
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def apply_rules(props: list[dict]) -> list[dict]:
    """1条件分のスクレイプ結果に対するルール適用。
    一般媒介の重複除外のみここで行う。
    同一現場グルーピングは検索条件をまたいで行う必要があるため merge_batch で処理する。"""
    before = len(props)
    props = _remove_ippan_duplicates(props)
    logger.info(f"ルール適用: {before}件 → {len(props)}件")
    return props


# ----------------------------------------------------------------
# ルール1: 一般媒介の重複除外
#   取引態様=一般 かつ 住所(丁目まで)+価格+面積 が一致 → 最初の1件だけ残す
# ----------------------------------------------------------------

def _remove_ippan_duplicates(props: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []

    for p in props:
        torihiki = p.get("取引態様", "")
        if "一般" not in torihiki:
            result.append(p)
            continue

        addr  = _chome(p.get("所在地", ""))
        price = _norm(p.get("価格", ""))
        area  = _norm(p.get("専有面積", "") or p.get("土地面積", ""))
        key   = (addr, price, area)

        if key not in seen:
            seen.add(key)
            result.append(p)

    removed = len(props) - len(result)
    if removed:
        logger.info(f"  一般媒介重複除外: {removed}件スキップ")
    return result


# ----------------------------------------------------------------
# ルール2: 同一現場グルーピング
#   会社名 + 住所(丁目まで・必須) + 沿線駅 + 徒歩分(必須) が全一致 → グループID を付与
#   いずれかが欠落していたらグループ化対象外
#   2件以上一致した場合のみ G001, G002... と振る
# ----------------------------------------------------------------

def _mark_same_site_groups(props: list[dict]) -> list[dict]:
    bucket: dict[tuple, list[int]] = defaultdict(list)

    for i, p in enumerate(props):
        company = p.get("会社名", "").strip()
        if not company:
            continue
        # 住所は厳密に「丁目まで」存在することを必須にする（丁目なしはグループ化対象外）
        chome = _chome_strict(p.get("所在地", ""))
        if not chome:
            continue
        walk = _walk_min(p.get("交通", ""))
        if not walk:
            continue
        station = _station(p.get("沿線駅", "") or p.get("交通", ""))
        key  = (company, chome, station, walk)
        bucket[key].append(i)

    gid = 1
    for indices in bucket.values():
        if len(indices) >= 2:
            label = f"G{gid:03d}"
            for i in indices:
                props[i]["グループID"] = label
            gid += 1

    return props


# ----------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------

def _chome(addr: str) -> str:
    """所在地から「町名＋丁目」レベルまでを抽出する。
    例:
      '千葉県市川市市川1丁目2-3'  → '千葉県市川市市川1丁目'
      '千葉県市川市市川5-2'        → '千葉県市川市市川'        (丁目なし・番地除去)
      '千葉県市川市市川５番地２'   → '千葉県市川市市川'
      '千葉県市川市市川'           → '千葉県市川市市川'
    """
    addr = (addr or "").strip()
    # 丁目を含む → 丁目までで切る
    m = re.search(r"^(.+?[\d０-９]+丁目)", addr)
    if m:
        return m.group(1)
    # 丁目なし → 末尾の番地（数字+ハイフン類+番地号の）を除去
    return re.sub(r"[\d０-９][\d０-９\-‐‑‒–—―－号番地の・]*$", "", addr).strip()


def _chome_strict(addr: str) -> str:
    """丁目までを厳密に抽出。丁目を含まない住所には空文字を返す（グループ化対象外を示す）。"""
    m = re.search(r"^(.+?[\d０-９]+丁目)", (addr or "").strip())
    return m.group(1) if m else ""


def _walk_min(kotsu: str) -> str:
    """交通フィールドから徒歩分を抽出する。例: '市川駅 徒歩5分' → '5'"""
    m = re.search(r"徒歩\s*(\d+)\s*分", kotsu)
    return m.group(1) if m else ""


def _station(text: str) -> str:
    """沿線駅または交通テキストから「駅」表現を抽出する。
    例: '東西線 西葛西 徒歩7分' → '東西線西葛西'
        '京葉線 海浜幕張'       → '京葉線海浜幕張'
    """
    s = (text or "").strip()
    # 徒歩部分や末尾の数字を除去
    s = re.sub(r"徒歩\s*\d+\s*分.*$", "", s).strip()
    s = re.sub(r"\d+分.*$", "", s).strip()
    # 空白を全部詰める（半角・全角）
    s = re.sub(r"[\s　]+", "", s)
    return s


def _norm(s: str) -> str:
    """数値文字列を正規化（カンマ・空白・単位を除去）"""
    return re.sub(r"[\s,、　円万㎡]", "", str(s)).strip()
