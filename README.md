# REINS物件監視システム

REINS（不動産流通標準情報システム）の検索結果を取得・差分検出してメール通知するツール。

## 主な機能

- 検索結果のExcel保存（物件DB / 成約・取消 / 変更ログの3シート構成）
- 新規物件・価格変更・取消候補の自動検出
- HTML形式の日次サマリーメール送信
- 図面PDFの自動DL・結合・印刷（B4白黒対応）
- 取消候補の猶予期間管理（3日経過で自動アーカイブ）

## 動作モード

| .bat | 用途 |
|---|---|
| 毎日.bat | 手動でログイン・検索 → スクリプトが結果パース（複数条件をループ） |
| 週次.bat | 毎日.batと同じUIで、最後に取消候補マーキングを実行 |
| 半自動毎日.bat | 手動ログイン後、config.jsonの条件を自動巡回 |
| 半自動週次.bat | 半自動毎日.batの週次版（取消候補マーキング付き） |
| 戻す.bat | 取消候補の物件をアクティブに戻す（電話確認後の運用） |
| メールテスト.bat | メール送信テスト |

## セットアップ

```bash
pip install -r requirements.txt
playwright install chromium
```

`config.example.json` を `config.json` にコピーして、ログイン情報・通知先・印刷設定を埋める。

### 印刷の B4 白黒設定（オプション）

[SumatraPDF](https://www.sumatrapdfreader.org) をインストールすると、`config.json` の `print` セクションに従って B4 白黒で自動印刷される。

## ファイル構成

- `monitor.py` ... モード切替・全体オーケストレーション
- `scraper.py` ... Playwrightで REINS を操作・パース
- `processor.py` ... DB読み書き・差分検出・状態管理
- `rules.py` ... 重複除外・同一現場グルーピングルール
- `mailer.py` ... HTMLメール本文生成・SMTP送信
- `pdf_handler.py` ... PDF結合・印刷・古いファイル削除
