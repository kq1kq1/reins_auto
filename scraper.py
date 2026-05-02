"""
REINSスクレイパー

流れ:
  1. REINSにログイン（ブラウザ保存済みのID/PWをチェックボックスで選択）
  2. お気に入り検索条件を名前で選択
  3. 実行モードに応じた日付フィルターをセット
       morning: 「日付を指定」チェック → 前日〜今日
       evening: 「当日」チェック → 今日のみ
       weekly:  日付フィルターなし（全体検索 → 取消検知用）
  4. 検索実行 → Excelダウンロード優先 / HTMLパースでフォールバック

【セレクタ確認手順】
  python monitor.py debug を実行するとブラウザが表示されます。
  各 "--- 要確認 ---" 箇所でF12を開き、実際の要素のセレクタを確認してください。
  debug_*.html も自動出力されます。
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, Page, Download

logger = logging.getLogger(__name__)


async def _human_wait(min_ms: int = 600, max_ms: int = 1400) -> None:
    """人が操作するような間隔でランダムに待機する"""
    ms = random.randint(min_ms, max_ms)
    await asyncio.sleep(ms / 1000)


class REINSScraper:
    def __init__(self, cfg: dict):
        self.reins_cfg   = cfg["reins"]
        self.browser_cfg = cfg.get("browser", {})
        self.export_dir  = Path(cfg["storage"].get("export_dir", "exports"))
        self.export_dir.mkdir(exist_ok=True)
        self.profile_dir = Path(cfg["storage"].get("profile_dir", "browser_profile"))
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.timeout       = self.browser_cfg.get("timeout_ms", 30000)
        self.wait_search   = self.browser_cfg.get("wait_ms_after_search", 3000)
        self.wait_cond     = self.browser_cfg.get("wait_ms_between_conditions", 2500)
        self.skip_zumen    = False  # bootstrap モードでTrueにすると図面DLをスキップ
        self._existing_ids: set[str] = set()  # PDF DL前に既存物件をスキップする為のセット
        self._seen_ippan_keys: set[tuple] = set()  # 一般媒介の重複検知用

    async def run(
        self,
        search_conditions: list[dict],
        run_mode: str,
    ) -> dict[str, list[dict]]:
        """
        全検索条件を実行して結果を返す。
        戻り値: {条件名: [物件dict, ...]}
        """
        results: dict[str, list[dict]] = {}

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.browser_cfg.get("headless", True),
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(self.timeout)

            try:
                await self._login(page)

                for condition in search_conditions:
                    name = condition["name"]
                    logger.info(f"検索条件: {name} [{run_mode}]")
                    try:
                        props = await self._run_condition(page, name, run_mode)
                        logger.info(f"  → {len(props)}件取得")
                        results[name] = props
                    except Exception as e:
                        logger.error(f"  → エラー: {e}", exc_info=True)
                        await page.screenshot(path=f"error_{name}.png")
                        results[name] = []

                    await asyncio.sleep(self.wait_cond / 1000)

            finally:
                await context.close()

        total = sum(len(v) for v in results.values())
        logger.info(f"合計取得: {total}件（{len(results)}条件）")
        return results

    # ----------------------------------------------------------------
    # 手動検索モード（ユーザーが画面操作 → スクリプトが結果をパース）
    # ----------------------------------------------------------------

    async def run_after_login(
        self,
        search_conditions: list[dict],
        run_mode: str,
        dl_zumen: bool,
        existing_ids: set[str] | None = None,
    ) -> list[tuple[str, list[dict]]]:
        """
        半自動モード: ユーザーが手動でログインした後、スクリプトが各条件を自動巡回する。

        フロー:
          1. ブラウザ起動 → ユーザーがログインを完了
          2. ターミナルで Enter
          3. スクリプトが各条件をid（番号）で選択 → 日付フィルタ → 検索 → パース
          4. 全条件処理後、ブラウザを閉じる

        run_mode: "morning" / "evening" / "weekly"
        """
        self.skip_zumen = not dl_zumen
        self._existing_ids = existing_ids or set()
        self._seen_ippan_keys.clear()
        results: list[tuple[str, list[dict]]] = []

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(self.timeout)

            try:
                await page.goto(self.reins_cfg["login_url"])
                print()
                print("=" * 60)
                print("ブラウザでログインを完了してください")
                print("（売買物件検索画面まで進んでおいてください）")
                print("ログイン完了後、ここで Enter を押すと自動巡回を開始します。")
                print("=" * 60)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, input)

                for condition in search_conditions:
                    name = condition.get("name", "")
                    cid  = condition.get("id")
                    label = f"{cid}: {name}" if cid is not None else name
                    logger.info(f"条件選択: {label} [{run_mode}]")
                    print(f"\n→ 条件「{label}」を実行中...")

                    try:
                        ok = await self._select_favorite(page, name, condition_id=cid)
                        if not ok:
                            logger.warning(f"  条件選択失敗: {label}")
                            results.append((name or str(cid), []))
                            continue

                        if run_mode != "weekly":
                            await self._set_date_filter(page, run_mode)

                        await _human_wait()
                        await page.click('//button[text()="検索"]')
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(self.wait_search / 1000)

                        props = await self._parse_all_tabs_and_pages(page, name or str(cid))
                        logger.info(f"  → {len(props)}件取得")
                        print(f"  ✔ {len(props)}件取得")
                        results.append((name or str(cid), props))

                        # 検索画面に戻る
                        await asyncio.sleep(self.wait_cond / 1000)
                        try:
                            await page.click('a:has-text("検索条件再設定"), button:has-text("検索条件再設定"), a:has-text("再設定")', timeout=3000)
                            await page.wait_for_load_state("networkidle")
                        except Exception:
                            await page.go_back()
                            await page.wait_for_load_state("networkidle")
                        await _human_wait()
                    except Exception as e:
                        logger.error(f"  条件エラー: {e}", exc_info=True)
                        await page.screenshot(path=f"error_{name or cid}.png")
                        results.append((name or str(cid), []))

                print("\n全条件巡回完了。ブラウザを閉じます...")
            finally:
                await context.close()

        return results

    async def run_manual_loop(
        self, dl_zumen: bool, existing_ids: set[str] | None = None,
    ) -> list[tuple[str, list[dict]]]:
        """
        ブラウザを1回だけ開き、ユーザーが複数条件を順に検索する。
        各検索の結果はEnter押下時にパースされ、最後にまとめて返る。

        フロー:
          1. ブラウザ起動 → ユーザーが REINS にログイン
          2. ユーザーが条件名をコンソールに入力
          3. ユーザーが画面で検索条件選択 → 検索ボタン
          4. 検索結果表示 → Enter
          5. スクリプトが結果をパース・記録
          6. 別の条件で2〜5を繰り返す
          7. 条件名に「終了」または空行を入れたらブラウザを閉じて完了

        Returns: [(condition_name, [props, ...]), ...]
        """
        self.skip_zumen = not dl_zumen
        self._existing_ids = existing_ids or set()
        self._seen_ippan_keys.clear()
        results: list[tuple[str, list[dict]]] = []

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(self.timeout)

            try:
                await page.goto(self.reins_cfg["login_url"])
                print()
                print("=" * 60)
                print("ブラウザが開きました。最初にREINSにログインしてください。")
                print("（ログイン後、検索画面まで進んでおいてください）")
                print("=" * 60)

                loop = asyncio.get_event_loop()

                while True:
                    print()
                    print("-" * 60)
                    cond = await loop.run_in_executor(
                        None,
                        input,
                        "条件名を入力（終了する場合は空Enter）: ",
                    )
                    cond = cond.strip()
                    if not cond or cond in ("終了", "exit", "quit", "q"):
                        break

                    print(f"  → 「{cond}」で検索してください。")
                    print("  → 結果が表示されたらここで Enter を押してください...")
                    await loop.run_in_executor(None, input)

                    logger.info(f"パース開始: {cond}")
                    try:
                        props = await self._parse_all_tabs_and_pages(page, cond)
                        logger.info(f"  取得: {len(props)}件")
                        print(f"  ✔ {len(props)}件取得しました")
                        results.append((cond, props))
                    except Exception as e:
                        logger.error(f"  パースエラー: {e}", exc_info=True)
                        print(f"  ✘ エラー: {e}")

                print("\nブラウザを閉じます...")
            finally:
                await context.close()

        return results

    async def run_manual(self, condition_name: str, dl_zumen: bool) -> list[dict]:
        """
        ブラウザを起動してユーザーが手動でログイン・検索する。
        検索結果が表示された状態でターミナルで Enter を押すと、
        現在のページから結果をパースする。
        """
        self.skip_zumen = not dl_zumen
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(self.timeout)

            try:
                await page.goto(self.reins_cfg["login_url"])
                print()
                print("=" * 60)
                print("ブラウザでログイン → 検索条件選択 → 検索を実行してください")
                print("検索結果が表示されたらここで Enter を押してください...")
                print("=" * 60)
                # 入力待ちの間ブラウザはユーザー操作可能
                await asyncio.get_event_loop().run_in_executor(None, input)

                logger.info(f"パース開始: {condition_name}")
                props = await self._parse_all_tabs_and_pages(page, condition_name)
                logger.info(f"取得: {len(props)}件")
                return props
            finally:
                await context.close()

    # ----------------------------------------------------------------
    # ログイン
    # ----------------------------------------------------------------

    async def _login(self, page: Page) -> None:
        logger.info("REINSログイン開始")
        await page.goto(self.reins_cfg["login_url"], wait_until="networkidle")

        # --- 要確認: ブラウザ保存済みID/PWの選択チェックボックス ---
        # ブラウザにID/PWが保存済みの場合、自動入力されたフィールドが表示される。
        # それを確認して選択するチェックボックス or 「このIDでログイン」ボタンのセレクタを確認する。
        #
        # パターンA: チェックボックスで保存済み認証情報を選ぶ場合
        #   saved_cb = await page.query_selector('input[type="checkbox"][name="savedLogin"]')
        #   if saved_cb:
        #       await saved_cb.check()
        #
        # パターンB: 自動入力されたフォームをそのまま送信する場合（何もしなくてよい）
        #
        # パターンC: ID/PWをコードから渡す場合
        #   await page.fill('input[name="loginID"]',  self.reins_cfg["username"])
        #   await page.fill('input[name="password"]', self.reins_cfg["password"])

        # 「所属機構の規程及びガイドラインを遵守します」チェックボックス
        # ラベルが上に重なっているので force=True で強制クリック
        await page.locator('.b-custom-control-lg input[type="checkbox"]').check(force=True)
        await _human_wait(500, 900)

        # ログインボタン（チェック後に有効化される）
        await page.click('button.btn-primary.btn-block')
        await page.wait_for_load_state("networkidle")
        await _human_wait()
        logger.info("ログイン完了")

        # 売買物件検索ボタンをクリック
        await page.click('//button[normalize-space(text())="売買 物件検索"]')
        await page.wait_for_load_state("networkidle")
        await _human_wait()
        logger.info("売買物件検索画面へ移動")

    # ----------------------------------------------------------------
    # 1条件分の検索・取得
    # ----------------------------------------------------------------

    async def _run_condition(
        self, page: Page, condition_name: str, run_mode: str
    ) -> list[dict]:

        # お気に入り検索条件を選択
        ok = await self._select_favorite(page, condition_name)
        if not ok:
            logger.warning(f"  お気に入り条件が見つかりませんでした: {condition_name}")
            html = await page.content()
            Path(f"debug_fav_{condition_name}.html").write_text(html, encoding="utf-8")
            return []

        # 日付フィルターをセット（weekly は不要）
        if run_mode != "weekly":
            await self._set_date_filter(page, run_mode)

        # 検索実行
        await _human_wait()
        await page.click('//button[text()="検索"]')
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(self.wait_search / 1000)

        # 件数確認
        count = await self._get_result_count(page)
        if count == 0:
            logger.info("  検索結果0件")
            return []
        logger.info(f"  検索結果: {count}件")

        # タブ×ページ全件パース（図面PDFもここでDL）
        return await self._parse_all_tabs_and_pages(page, condition_name)

    # ----------------------------------------------------------------
    # お気に入り検索条件の選択
    # ----------------------------------------------------------------

    async def _select_favorite(self, page: Page, condition_name: str, condition_id: int | None = None) -> bool:
        """
        検索条件のドロップダウンから保存済み条件を選択して読み込む。

        condition_id（番号）が指定されればそれで厳密一致。
        指定されなければ condition_name で部分一致。
        """
        # 「検索条件を表示」が隠れていたら開く（トグルボタン）
        show_btn = await page.query_selector('a:has-text("検索条件を表示")')
        if show_btn:
            await _human_wait()
            await show_btn.click()
            await _human_wait()

        # ドロップダウンを取得
        sel = await page.query_selector('select.p-selectbox-input.custom-select')
        if not sel:
            logger.warning("保存検索条件のドロップダウンが見つかりません")
            html = await page.content()
            Path(f"debug_wt_{condition_name}.html").write_text(html, encoding="utf-8")
            return False

        # オプションから条件を探す（id優先、なければ部分一致）
        options = await sel.query_selector_all('option')
        matched_value = None
        for opt in options:
            text = (await opt.inner_text()).strip()
            if condition_id is not None:
                # "48: 江戸川区マンション" の頭の番号が一致
                if re.match(rf'^\s*{condition_id}\s*:', text):
                    matched_value = await opt.get_attribute('value')
                    logger.debug(f"条件マッチ(ID={condition_id}): '{text}'")
                    break
            else:
                if condition_name and condition_name in text:
                    matched_value = await opt.get_attribute('value')
                    logger.debug(f"条件マッチ: '{text}'")
                    break

        if matched_value is None:
            logger.warning(f"条件名が見つかりません: {condition_name}")
            html = await page.content()
            Path(f"debug_wt_{condition_name}.html").write_text(html, encoding="utf-8")
            return False

        await _human_wait()
        await sel.select_option(value=matched_value)
        await _human_wait()

        # 「読込」ボタンをクリック
        await page.click('//button[normalize-space(text())="読込"]')
        await _human_wait()

        # 「検索条件を読込みました。」ダイアログのOKをクリック
        try:
            ok_btn = page.locator('.modal-footer button.btn-primary')
            await ok_btn.wait_for(timeout=5000)
            await ok_btn.click()
            await page.wait_for_load_state("networkidle")
            await _human_wait()
        except Exception:
            pass  # ダイアログが出ない場合はそのまま続行

        logger.debug(f"検索条件を読み込みました: {condition_name}")
        return True

    # ----------------------------------------------------------------
    # 日付フィルターのセット
    # ----------------------------------------------------------------

    async def _set_date_filter(self, page: Page, run_mode: str) -> None:
        """
        morning: 「日付を指定」ラジオ → 前日〜今日
        evening: 「当日」ラジオ
        """
        if run_mode == "evening":
            await _human_wait()
            await page.click('//label[normalize-space(text())="当日"]/../input[@type="radio"]')

        elif run_mode == "morning":
            await _human_wait()
            await page.click('//label[normalize-space(text())="日付を指定"]/../input[@type="radio"]')
            await _human_wait(400, 800)

            today     = datetime.now()
            yesterday = today - timedelta(days=1)
            await self._fill_date_range(page, from_date=yesterday, to_date=today)

        logger.debug(f"日付フィルター設定: {run_mode}")

    async def _fill_date_range(self, page: Page, from_date: datetime, to_date: datetime) -> None:
        """
        登録年月日の開始日・終了日を元号/年/月/日で入力する。
        「令和(R)オプションを持つselect」を起点に隣接inputへ入力する。
        """
        # 令和オプションを持つselectを全取得 → 最初の2つが開始日・終了日
        era_selects = await page.query_selector_all('select:has(option[value="R"])')
        if len(era_selects) < 2:
            logger.warning("日付入力フィールドが見つかりません")
            return

        for date, era_sel in zip([from_date, to_date], era_selects[:2]):
            reiwa_year = date.year - 2018

            # 元号を令和にセット
            await era_sel.select_option(value='R')
            await _human_wait(200, 400)

            # 同じ親div内のinputに年・月・日を入力（JavaScriptで一括）
            await page.evaluate(
                """([sel, y, m, d]) => {
                    const parent = sel.closest('div') || sel.parentElement;
                    const inputs = parent.querySelectorAll('input');
                    const trigger = (el, val) => {
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    };
                    if (inputs[0]) trigger(inputs[0], y);
                    if (inputs[1]) trigger(inputs[1], m);
                    if (inputs[2]) trigger(inputs[2], d);
                }""",
                [era_sel, str(reiwa_year), str(date.month), str(date.day)]
            )
            await _human_wait(300, 500)

    # ----------------------------------------------------------------
    # 全タブ × 全ページをパース
    # ----------------------------------------------------------------

    async def _parse_all_tabs_and_pages(
        self, page: Page, condition_name: str
    ) -> list[dict]:
        """タブ（売マンション・売一戸建・売土地など）ごとに全ページを取得する。"""
        all_props: list[dict] = []

        tabs = await page.query_selector_all('a[role="tab"]')
        if not tabs:
            return await self._parse_all_pages(page, condition_name)

        for tab in tabs:
            tab_text = (await tab.inner_text()).strip()
            # 件数0のタブはスキップ（"(0件)" or "(0)" のみマッチ。"(40件)"等は除外）
            if re.search(r'\(\s*0\s*(件)?\s*\)', tab_text):
                continue
            await _human_wait()
            await tab.click()
            await page.wait_for_load_state("networkidle")
            await _human_wait(500, 1000)

            tab_props = await self._parse_all_pages(page, condition_name)
            logger.info(f"    タブ「{tab_text}」: {len(tab_props)}件")
            all_props.extend(tab_props)

        return all_props

    async def _parse_all_pages(self, page: Page, condition_name: str) -> list[dict]:
        props    = []
        page_num = 1

        while True:
            on_page = await self._parse_result_page(page, condition_name)
            props.extend(on_page)

            has_next = await self._go_next_page(page)
            if not has_next:
                break

            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(self.wait_search / 1000)
            page_num += 1
            logger.debug(f"    ページ {page_num}")

        return props

    async def _parse_result_page(self, page: Page, condition_name: str) -> list[dict]:
        """
        検索結果1ページ分をパースする。
        ヘッダー行から列名→インデックスのマップを動的に作って取得するので、
        物件種別（マンション・戸建・土地）が違っても自動対応する。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        props = []

        # ヘッダー行から列マッピング作成
        header_map = await self._build_header_map(page)

        rows = await self._find_result_rows(page)
        if not rows:
            html = await page.content()
            fname = f"debug_result_{condition_name}_{datetime.now():%H%M%S}.html"
            Path(fname).write_text(html, encoding="utf-8")
            logger.warning(f"結果行が見つかりません → {fname} を確認してください")
            return []

        logger.info(f"  検出行数: {len(rows)}  ヘッダー: {len(header_map)}列")

        for ri, row in enumerate(rows):
            try:
                cells = await row.query_selector_all('.p-table-body-item, td, [class*="body-item"]')
                if len(cells) < 8:
                    if ri == 0:
                        logger.warning(f"  セル数が想定より少ない: {len(cells)}")
                    continue

                texts = [await _cell_text(cells, i) for i in range(len(cells))]

                def col(name: str, *aliases: str) -> str:
                    """列名（または別名）でテキスト取得。"""
                    for n in (name, *aliases):
                        idx = header_map.get(n)
                        if idx is not None and idx < len(texts):
                            return texts[idx].replace("\n", " ").strip()
                    return ""

                prop_id = col("物件番号")
                if not prop_id:
                    # ヘッダーマップが取れなかった場合のフォールバック（cell[3]）
                    prop_id = _safe_get(texts, 3)
                if not prop_id:
                    continue

                full_text = "\n".join(texts)

                # PDF DL判定: skip_zumen=False かつ 既存DBになく、かつ一般媒介の重複でもない場合のみDL
                pdf_path = ""
                should_dl = (
                    not self.skip_zumen
                    and prop_id not in self._existing_ids
                )
                if should_dl:
                    torihiki = col("取引態様")
                    if "一般" in torihiki:
                        ippan_key = (
                            _chome(col("所在地")),
                            _norm_key(col("価格")),
                            _norm_key(col("専有面積") or col("土地面積") or col("建物面積")),
                        )
                        if ippan_key in self._seen_ippan_keys:
                            should_dl = False
                        else:
                            self._seen_ippan_keys.add(ippan_key)

                if should_dl:
                    zumen_btn = await row.query_selector('button:has-text("図面")')
                    if zumen_btn:
                        pdf_path = await self._download_zumen(page, zumen_btn, prop_id)

                prop = {
                    "物件番号":   prop_id,
                    "物件種別":   col("物件種目") or condition_name,
                    "取引状況":   col("取引状況"),
                    "取引態様":   col("取引態様"),
                    "所在地":     col("所在地"),
                    "建物名":     col("建物名"),
                    "所在階":     col("所在階"),
                    "間取り":     col("間取", "間取り") or _extract_madori(full_text),
                    "専有面積":   _extract_area(col("専有面積")),
                    "建物面積":   _extract_area(col("建物面積")),
                    "土地面積":   _extract_area(col("土地面積")),
                    "価格":       _extract_price(col("価格") or full_text),
                    "㎡単価":     col("㎡単価"),
                    "坪単価":     col("坪単価"),
                    "管理費":     col("管理費"),
                    "用途地域":   col("用途地域"),
                    "建ぺい率":   col("建ぺい率"),
                    "容積率":     col("容積率"),
                    "接道状況":   col("接道状況"),
                    "接道１":     col("接道１", "接道1"),
                    "沿線駅":     col("沿線駅"),
                    "交通":       col("交通"),
                    "築年月":     col("築年月"),
                    "会社名":     _extract_company(col("商号")),
                    "電話番号":   col("電話番号"),
                    "登録日":     _extract_date(full_text),
                    "グループID": "",
                    "初回取得日": today,
                    "最終確認日": today,
                    "_pdf_path":  pdf_path,
                }
                props.append(prop)

            except Exception as e:
                logger.debug(f"行パースエラー（スキップ）: {e}")

        return props

    async def _build_header_map(self, page: Page) -> dict[str, int]:
        """ヘッダー行から「列名 → セルインデックス」のマップを作る。"""
        header_map: dict[str, int] = {}
        for sel in (
            '.p-table-header-row .p-table-header-item',
            '.p-table-header-item',
            '[class*="table-header-item"]',
            'th',
        ):
            headers = await page.query_selector_all(sel)
            if headers:
                for i, h in enumerate(headers):
                    try:
                        t = (await h.inner_text()).strip().replace("　", " ")
                        if t and t not in header_map:
                            header_map[t] = i
                    except Exception:
                        continue
                if header_map:
                    break
        return header_map

    async def _find_result_rows(self, page: Page) -> list:
        """検索結果の行を複数セレクタで探す（REINS構造変化への耐性）。"""
        for sel in (
            '.p-table-body-row',
            'div[class*="table-body-row"]',
            'tr.p-table-body-row',
            'tr[class*="body-row"]',
        ):
            rows = await page.query_selector_all(sel)
            if rows:
                logger.debug(f"  行セレクタ採用: {sel}")
                return rows
        return []

    async def _download_zumen(self, page: Page, btn, prop_id: str) -> str:
        """図面ボタンをクリックしてPDFをダウンロードする。パスを返す。"""
        safe_id   = re.sub(r'[\\/:*?"<>|]', "_", prop_id)
        save_path = self.export_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_id}.pdf"
        try:
            async with page.expect_download(timeout=10000) as dl_info:
                await btn.click()
            dl: Download = await dl_info.value
            await dl.save_as(str(save_path))
            logger.debug(f"  図面DL: {prop_id}")
            return str(save_path)
        except Exception as e:
            logger.debug(f"  図面DLスキップ {prop_id}: {e}")
            return ""

    async def _get_result_count(self, page: Page) -> int:
        try:
            el = await page.query_selector('.p-search-result-count, .result-count')
            if el:
                t = await el.inner_text()
                m = re.search(r"(\d[\d,]*)", t)
                if m:
                    return int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return -1

    async def _go_next_page(self, page: Page) -> bool:
        """次ページボタンをクリック。次ページがあれば True を返す。"""
        try:
            btn = await page.query_selector(
                '.page-item:not(.disabled) [aria-label="Go to next page"]'
            )
            if btn:
                await _human_wait()
                await btn.click()
                return True
        except Exception:
            pass
        return False


# ----------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------

async def _cell_text(cells: list, idx: int) -> str:
    if idx >= len(cells):
        return ""
    try:
        t = await cells[idx].inner_text()
        return t.strip().replace("　", " ")
    except Exception:
        return ""


def _safe_get(texts: list[str], idx: int) -> str:
    """インデックスからテキストを安全に取得し、改行を空白に変換する。"""
    if idx >= len(texts):
        return ""
    return texts[idx].replace("\n", " ").strip()


def _extract_price(text: str) -> str:
    """テキストから価格（万円単位の数字）を抽出する。"""
    text = text.replace(",", "").replace("　", "")
    # X,XXX万円 / XXX万 のパターン
    m = re.search(r"(\d+(?:\.\d+)?)\s*万円?", text)
    if m:
        return m.group(1)
    return ""


def _extract_area(text: str) -> str:
    """専有面積セルから面積（㎡）の数値だけを抽出する。"""
    text = text.replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m2|平米)", text)
    if m:
        return m.group(1)
    # ㎡記号がない場合は最初の数字
    m = re.search(r"^\s*(\d+(?:\.\d+)?)", text)
    return m.group(1) if m else text.split("\n")[0].strip()


def _extract_madori(text: str) -> str:
    """テキストから間取り（1K, 2LDK等）を抽出する。"""
    m = re.search(r"\b(\d+(?:S?LDK|S?DK|S?LK|S?K|R))\b", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_date(text: str) -> str:
    """登録日らしい日付を抽出する（YYYY/MM/DD or YYYY-MM-DD or 令和X年X月X日）。"""
    m = re.search(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})", text)
    if m:
        return m.group(1).replace("/", "-")
    m = re.search(r"(令和\d+年\d+月\d+日|平成\d+年\d+月\d+日)", text)
    return m.group(1) if m else ""


def _extract_company(cell_text: str) -> str:
    """電話番号セルから会社名（商号）を抽出する。複数行の場合は最初の非数字行。"""
    if not cell_text:
        return ""
    for line in cell_text.split("\n"):
        line = line.strip()
        # 電話番号らしい行（数字とハイフンが大半）はスキップ
        if not line:
            continue
        digit_ratio = sum(c.isdigit() or c == "-" for c in line) / max(len(line), 1)
        if digit_ratio < 0.5:
            return line
    return cell_text.split("\n")[0].strip()


def _chome(addr: str) -> str:
    """所在地から丁目までを抽出する（一般媒介重複判定用）。"""
    m = re.search(r"^(.+?\d+丁目)", addr or "")
    return m.group(1) if m else (addr or "").strip()


def _norm_key(s: str) -> str:
    """数値文字列を正規化（一般媒介重複判定用）。"""
    return re.sub(r"[\s,、　円万㎡]", "", str(s)).strip()


def _normalize_price(raw: str) -> str:
    raw = raw.replace(",", "").replace("　", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)", raw)
    return m.group(1) if m else raw
