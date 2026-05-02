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
    before = len(props)
    props = _remove_ippan_duplicates(props)
    props = _mark_same_site_groups(props)
    logger.info(f"ルール適用: {before}件 → {len(props)}件 （グループ付与済み）")
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
#   会社名 + 住所(丁目まで) + 徒歩分 が全一致 → グループID を付与
#   2件以上一致した場合のみ G001, G002... と振る
# ----------------------------------------------------------------

def _mark_same_site_groups(props: list[dict]) -> list[dict]:
    bucket: dict[tuple, list[int]] = defaultdict(list)

    for i, p in enumerate(props):
        company = p.get("会社名", "").strip()
        if not company:
            continue
        addr = _chome(p.get("所在地", ""))
        walk = _walk_min(p.get("交通", ""))
        key  = (company, addr, walk)
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
    """所在地から丁目までを抽出する。例: '千葉県市川市市川1丁目2-3' → '千葉県市川市市川1丁目'"""
    m = re.search(r"^(.+?\d+丁目)", addr)
    return m.group(1) if m else addr.strip()


def _walk_min(kotsu: str) -> str:
    """交通フィールドから徒歩分を抽出する。例: '市川駅 徒歩5分' → '5'"""
    m = re.search(r"徒歩\s*(\d+)\s*分", kotsu)
    return m.group(1) if m else ""


def _norm(s: str) -> str:
    """数値文字列を正規化（カンマ・空白・単位を除去）"""
    return re.sub(r"[\s,、　円万㎡]", "", str(s)).strip()
