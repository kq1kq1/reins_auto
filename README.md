# REINS物件監視システム

REINS（不動産流通標準情報システム）の検索結果を取得し、Excelに蓄積して
**新規物件・価格変更・公開停止（成約/取消）** を自動検知してメール通知するツール。

- ログインと検索は人が操作（規約対応）、**結果のパース以降を自動化**する「半自動」方式
- 図面PDFを新規物件だけ自動ダウンロード → 結合 → B4白黒で印刷
- 物件の重複除外・同一現場グルーピング・再登録検知などを内蔵

---

## 1. 動作フロー概要

1. `.bat` をダブルクリック → ブラウザ（Chromium）が起動
2. 自分でREINSにログイン → 売買物件検索画面まで進める
3. ターミナルで Enter → スクリプトが config.json の検索条件を**番号で自動選択**して巡回
4. 各検索結果をパース → Excel DB（`reins_db.xlsx`）に蓄積・差分検出
5. 新規/価格変更/公開停止をメール通知、新規物件の図面PDFを印刷

> ### ⚠️ 無人での全自動実行はしません
> REINSの利用規約上、ログインから検索までを無人で自動実行することは認められていません。
> 本ツールは **ログイン〜「売買物件検索」画面までを人が手動で操作**し、その後の
> 検索条件の巡回・結果パース・集計・通知・印刷だけを自動化する「半自動」方式です。
> そのため Windowsタスクスケジューラ等での**無人スケジュール実行はできません**
> （PCの前で人が操作して起動する前提）。

---

## 2. ファイル構成

| ファイル | 役割 |
|---|---|
| `monitor.py` | エントリポイント。モード切替・全体の制御 |
| `scraper.py` | Playwrightでブラウザ操作・検索結果パース |
| `processor.py` | Excel DB読み書き・差分検出・重複/再登録判定 |
| `rules.py` | 一般媒介の重複除外・同一現場グルーピング |
| `mailer.py` | HTMLメール生成・SMTP送信 |
| `pdf_handler.py` | 図面PDF結合・印刷・古いファイル削除 |
| `config.example.json` | 設定ファイルのひな形（これをコピーして使う） |
| `requirements.txt` | 依存ライブラリ |
| `*.bat` | 各モードのワンクリック起動 |

### 実行用バッチ

| .bat | モード | 用途 |
|---|---|---|
| **半自動新規取得.bat** | half_morning | 日々の取得（前回実行日〜当日の新着）。**メイン** |
| **半自動クリーニング.bat** | half_weekly | 週次の全件検索→公開停止検知。週2回程度実行 |
| **クリーンアップ.bat** | cleanup | DBメンテ（グループID再計算・誤成約削除）。必要時 |
| **戻す.bat** | restore | 取消候補をアクティブに戻す（電話確認後など） |
| **成約確定.bat** | confirm | 指定物件を手動で成約・取消シートへ移動 |
| **メールテスト.bat** | test_mail | メール送信設定の確認 |

---

## 3. セットアップ（初回・新しいマシン共通）

### 3-1. Python と依存ライブラリ

```powershell
# Python 3.13 をインストール（python.org / "Add to PATH" にチェック）
pip install -r requirements.txt
playwright install chromium
```

### 3-2. 設定ファイルを作成

`config.example.json` を `config.json` にコピーして中身を埋める（`config.json` はGit管理外）。

```powershell
copy config.example.json config.json
```

詳細は「4. config.json の項目」を参照。

### 3-3. 図面印刷（任意・B4白黒にしたい場合）

[SumatraPDF](https://www.sumatrapdfreader.org) をインストールすると、`config.json` の
`print` 設定（B4・モノクロ）で自動印刷される。未インストールの場合はWindowsの
デフォルトプリンタ設定で印刷される。

---

## 4. config.json の項目

```jsonc
{
  "reins": {
    "login_url": "https://system.reins.jp/main/BFC/EA/BFCEAD001.html",
    "username": "",   // 自動ログインは使わないため空でOK
    "password": ""
  },

  // 日々の取得で巡回するREINS保存検索の「番号」
  "search_conditions": [
    {"id": 26, "name": ""},
    {"id": 27, "name": ""}
  ],

  // 週次クリーニング専用の保存検索の番号（空なら search_conditions を使う）
  // ※ DB全体をカバーする全エリア版にすること（カバー漏れは誤検知の原因）
  "weekly_search_conditions": [
    {"id": 35, "name": ""}
  ],

  "storage": {
    "db_path": "reins_db.xlsx",
    "export_dir": "exports",
    "log_path": "reins_auto.log",
    "profile_dir": "C:/Users/＜ユーザー名＞/AppData/Local/reins_auto/browser_profile",
    "pdf_keep_days": 2,            // 図面PDFを残す日数
    "removal_confirm_misses": 2,  // 週次で連続N回見つからなければ成約確定
    "removal_min_coverage": 0.7   // 取得が DB の70%未満なら取消検知をスキップ
  },

  "notification": {
    "email_from": "xxx@gmail.com",
    "email_to": ["a@example.com", "b@example.com"],  // 文字列でもリストでも可
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_password": "Gmailのアプリパスワード",
    "send_daily_summary": true
  },

  "print": {
    "paper_size": "B4",
    "color_mode": "monochrome",
    "printer_name": "",     // 空ならデフォルトプリンタ
    "sumatra_path": ""      // 空なら自動検出
  },

  "browser": {
    "headless": false,
    "timeout_ms": 30000,
    "wait_ms_after_search": 3000,
    "wait_ms_between_conditions": 2500
  }
}
```

### Gmailアプリパスワードの取得
Googleアカウント → セキュリティ → 2段階認証を有効化 → 「アプリパスワード」を発行し
`smtp_password` に設定（通常のログインパスワードは使用不可）。

### REINS保存検索（ワンタッチ検索）の番号
`id` はREINSの「検索条件の選択・保存」ドロップダウンの**先頭番号**（例: `48: 江戸川区マンション` の `48`）。
新しいマシンでも**同じアカウント**なら同じ番号。別アカウントだと番号が変わるので確認・修正が必要。

---

## 5. ⚠️ 新しいマシンで使うとき追加で必要な作業

Gitには**コードのみ**入っており、以下は各マシンで個別に用意する必要があります。

| 項目 | 内容 |
|---|---|
| **config.json** | リポジトリに含まれない（パスワード保護）。`config.example.json` からコピーして作成 |
| **profile_dir のパス** | `C:/Users/＜ユーザー名＞/...` の**ユーザー名を必ずそのマシンに合わせて変更**。間違えるとブラウザが起動しない |
| **ブラウザのログイン** | `browser_profile` はマシンごとに作られる。初回は手動でREINSにログインが必要（次回以降は保持される） |
| **Gmailアプリパスワード** | `smtp_password` に設定。`メールテスト.bat`で確認 |
| **SumatraPDF** | B4白黒印刷したい場合にインストール（任意） |
| **REINS保存検索の番号** | `search_conditions` / `weekly_search_conditions` の `id` がそのアカウントの番号と合っているか確認 |
| **reins_db.xlsx** | リポジトリに含まれない。新規なら空から自動作成。既存DBを引き継ぐ場合は手動でコピー |
| **OneDrive配下を避ける** | `profile_dir` はOneDrive同期対象外（AppData配下推奨）。同期するとブラウザ起動に失敗する |

---

## 5-2. Googleスプレッドシートでデータ共有する（チーム運用）

DBをExcelファイルではなく**Googleスプレッドシート**にすると、複数人が同じデータを
共有して使える。`config.json` の `storage.backend` を `"sheets"` にするだけで切替可能
（`"excel"` に戻せばローカルExcelに戻る。**コードは共通・両対応**）。

### 一度きりの準備（Google側）

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
2. **Google Sheets API** と **Google Drive API** を有効化
3. **サービスアカウント**を作成 → **JSONキー**をダウンロードし、
   プロジェクトフォルダに `service_account.json` として置く（gitには入らない）
4. Googleスプレッドシートを新規作成し、**サービスアカウントのメールアドレス**
   （JSON内の `client_email`）に「**編集者**」で共有
5. スプレッドシートのURL `…/d/●●●/edit` の **●●● がスプレッドシートID**

### config.json 設定

```jsonc
"storage": {
  "backend": "sheets",
  "spreadsheet_id": "スプレッドシートID",
  "service_account_json": "service_account.json"
}
```

### 依存ライブラリ

```powershell
pip install gspread google-auth
```

### 各マシンで必要なもの

- コード（git clone）
- `config.json`（backend=sheets、同じ spreadsheet_id）
- `service_account.json`（**全員同じものを安全に配布**。gitには含めない）

### 注意

- **同時実行しない**：2人が同時に走らせると後勝ちで上書きの可能性
- **色分け**：Sheetsでは「表示形式 → 条件付き書式」で一度設定すると自動反映
- **図面PDF印刷**は実行した人のマシンでローカルに行われる（従来通り）

---

## 6. 運用の流れ（推奨）

### 日々
- **半自動新規取得.bat** を朝・夕に実行
  - 朝：前回実行日〜当日の新着を取得（休み明けは最後の実行日からの差分を全部拾う）
  - 同じ日の2回目は当日分のみになる
  - 新規物件はメール通知＋図面PDFを印刷

### 週2回（例：火・金、数日空けて）
- **半自動クリーニング.bat** を実行
  - 全件検索でDB全体と突き合わせ → 見つからない物件を「取消候補」に
  - **2回連続**（`removal_confirm_misses`）で見つからなければ「成約・取消」確定
  - 同じ現場（会社名・住所丁目・沿線駅・徒歩分が一致）の物件はグループ化し最安1件のみ通知

### 必要時
- **戻す.bat**：取消候補が実はまだ販売中だった場合、物件番号を入力してアクティブに戻す
- **成約確定.bat**：手動で成約・取消シートへ移動
- **クリーンアップ.bat**：DBの整合性メンテ（グループID再計算・誤成約削除・並び順整理）

---

## 7. Excel DB（reins_db.xlsx）の構成

| シート | 内容 |
|---|---|
| **物件DB** | 現在アクティブな物件＋取消候補（「状態」列で区別。取消候補は赤背景） |
| **成約・取消** | 成約・取消が確定した物件のアーカイブ（「成約・取消日」列に確定日） |
| **変更ログ** | 新規/価格変更/取消候補/成約確定/物件番号変更などの履歴 |

「状態」列：`アクティブ` / `取消候補`。「未検出回数」列：週次で連続して見つからなかった回数。

---

## 8. 内部ロジックの要点

- **重複除外**：取引態様=一般 で住所・価格・面積が一致するものは最初の1件のみ
- **同一現場グルーピング**：会社名＋住所（丁目／町名）＋沿線駅＋徒歩分が完全一致なら同一現場とみなし、最安1件だけ通知・印刷
- **再登録検知**：物件番号が変わっても、種別・所在地・建物名・所在階・間取り・面積・築年月が一致すれば同一物件として物件番号を更新（新規扱いしない）。ただし旧番号が同じ検索結果にまだ存在する場合は別物件として扱う
- **表記ゆれ吸収**：全角半角・記号・カタカナ濁点などを正規化して比較
- **公開停止検知**：週次の全件検索でDB全体と突き合わせ。取得がDBの70%未満なら誤検知防止のためスキップ

---

## 9. トラブルシュート

| 症状 | 対処 |
|---|---|
| ブラウザが起動直後に落ちる | `profile_dir` がOneDrive配下になっていないか確認。AppData配下に変更 |
| メール送信失敗 | `メールテスト.bat`で確認。Gmailアプリパスワード・宛先を確認 |
| 取得0件・列ズレ | `debug_*.html` が出力される。REINSの画面構成変更の可能性 |
| 図面が印刷されない | 図面DLを「y」で実行したか／新規物件があったか／SumatraPDFの有無を確認 |
| 取消候補が多すぎる | 週次のカバレッジ低下（500件上限など）。検索条件を分割してエリアを細分化 |

---

## 10. リポジトリ

https://github.com/kq1kq1/reins_auto

`config.json`・`reins_db.xlsx`・`browser_profile/`・ログ・PDF・デバッグ出力は
`.gitignore` で除外（認証情報・個人データのため）。
