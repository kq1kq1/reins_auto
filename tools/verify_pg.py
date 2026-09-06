"""
スプレッドシートと Postgres(Supabase) の内容が一致しているか突合する。

読み取り専用。どちらにも書き込まない。
二重書き込みを始めたあと、定期的に流して「ずれていないか」を見るのに使う。

使い方:
    python tools/verify_pg.py
"""

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import processor          # noqa: E402
import pg_backend         # noqa: E402

# 内容比較する列（日本語列名 → DB列名）
CHECK_COLS = {
    "所在地": "address",
    "価格": "price",
    "間取り": "layout",
    "会社名": "agency_name",
    "最終確認日": "last_seen_on",
}
SAMPLE = 200          # 内容を突き合わせる件数


def _norm(v):
    """比較用にそろえる（Noneと空文字、数値の表記ゆれを吸収）"""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("None", "NaT"):
        return ""
    # 3480 と 3480.0 を同じとみなす
    try:
        f = float(s.replace(",", ""))
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return s


def main():
    cfg_all = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    pg_cfg = cfg_all["postgres"]
    processor.configure_storage(cfg_all["storage"])
    db_path = cfg_all["storage"]["db_path"]

    print("=" * 60)
    print(" スプレッドシート ⇔ Supabase 突合")
    print("=" * 60)

    db_df = processor.load_db(db_path)
    ar_df = processor.load_archive(db_path)

    sheet = {}          # 物件番号 -> (期待する状態, 元レコード)
    dups = []
    for rec in db_df.to_dict("records"):
        pid = str(rec.get("物件番号", "")).strip()
        if pid:
            sheet[pid] = (str(rec.get("状態", "")).strip(), rec)
    for rec in ar_df.to_dict("records"):
        pid = str(rec.get("物件番号", "")).strip()
        if not pid:
            continue
        if pid in sheet:
            dups.append(pid)
            continue
        sheet[pid] = (pg_backend.STATUS_CLOSED, rec)

    print(f"[1] スプシ側  物件DB {len(db_df):,} + 成約取消 {len(ar_df):,}"
          f" − 重複 {len(dups)} = {len(sheet):,}件")

    with pg_backend.connect(pg_cfg) as conn:
        with conn.cursor() as cur:
            cur.execute("select property_no, status from reins.properties")
            pg = dict(cur.fetchall())
            print(f"[2] Postgres側 {len(pg):,}件")

            # ── 件数と過不足 ──
            missing = set(sheet) - set(pg)
            extra = set(pg) - set(sheet)
            print(f"[3] 過不足     Postgresに無い {len(missing)}件 / "
                  f"スプシに無い {len(extra)}件"
                  f"  {'OK' if not missing and not extra else '★要確認'}")
            for p in list(missing)[:5]:
                print(f"      未投入: {p}")
            for p in list(extra)[:5]:
                print(f"      余分  : {p}")

            # ── 状態の一致 ──
            bad = [(p, s, pg[p]) for p, (s, _) in sheet.items()
                   if p in pg and s != pg[p]]
            print(f"[4] 状態の一致 不一致 {len(bad)}件"
                  f"  {'OK' if not bad else '★要確認'}")
            for p, s, g in bad[:5]:
                print(f"      {p}: スプシ={s} / PG={g}")

            # ── 内容の突合（ランダム抽出）──
            targets = random.sample(sorted(set(sheet) & set(pg)),
                                    min(SAMPLE, len(set(sheet) & set(pg))))
            cols = ", ".join(CHECK_COLS.values())
            cur.execute(
                f"select property_no, {cols} from reins.properties "
                f"where property_no = any(%s)", (targets,))
            got = {r[0]: r[1:] for r in cur.fetchall()}

            diff = 0
            shown = 0
            for pid in targets:
                rec = sheet[pid][1]
                for i, (jp, en) in enumerate(CHECK_COLS.items()):
                    a, b = _norm(rec.get(jp)), _norm(got[pid][i])
                    if a != b:
                        diff += 1
                        if shown < 5:
                            print(f"      {pid} {jp}: スプシ={a!r} / PG={b!r}")
                            shown += 1
            checked = len(targets) * len(CHECK_COLS)
            print(f"[5] 内容の突合 {len(targets)}件×{len(CHECK_COLS)}項目={checked}箇所中 "
                  f"不一致 {diff}箇所  {'OK' if diff == 0 else '★要確認'}")

    # ── 重複の中身 ──
    if dups:
        print("-" * 60)
        print(f"[参考] 物件DBと成約・取消の両方にある物件番号: {len(dups)}件")
        print("       本来どちらか一方のはず。スプシで2シート管理している弊害。")
        for p in dups[:5]:
            print(f"       {p}")
        if len(dups) > 5:
            print(f"       …他{len(dups) - 5}件")
    print("=" * 60)


if __name__ == "__main__":
    main()
