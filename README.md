# river-logger

川の防災情報(https://www.river.go.jp/kawabou/pcfull/tm?...)の観測ページを
1時間ごとに自動取得し、`data/water_level_log.csv` に蓄積するツールです。
GitHub Actions 上で動くため、**自分のPCを起動しておく必要はありません。**

## セットアップ手順（GitHubアカウントが必要です・無料）

1. GitHub (https://github.com) にログイン（アカウントがなければ無料登録）。
2. 新しいリポジトリを作成する（Public / Private どちらでもOK。Privateでも
   GitHub Actionsの無料枠は使えます）。
3. このフォルダの中身（`scrape.py`, `requirements.txt`, `README.md`,
   `.github/workflows/scrape.yml`）をそのままリポジトリにアップロードする。
   - GitHubの「Add file」→「Upload files」からドラッグ&ドロップでOK。
   - `.github/workflows/scrape.yml` はフォルダ構造を保ったままアップロードすること。
4. リポジトリの「Settings」→「Actions」→「General」→
   「Workflow permissions」を **"Read and write permissions"** に変更して保存。
   （これをしないと自動コミットができません）
5. 「Actions」タブを開くと `Hourly river data scrape` というワークフローが
   表示されるので、一度「Run workflow」で手動実行して動作確認する。
6. 成功すると `data/water_level_log.csv` が更新される。以降は
   `cron: "0 * * * *"` の設定により、毎時0分(UTC / 日本時間毎時0分)に
   自動実行され続けます。

## 監視対象を変える場合

`scrape.py` 冒頭の `TARGET_URL` を書き換えれば、別の観測所・別の項目
（雨量など）にも流用できます。

## 注意点

- 取得したいのは水位テーブルですが、ページ構成によっては表の中に不要な
  行が混ざることがあります。`data/last_page.html` に毎回最新の取得結果
  (生HTML)を保存しているので、CSVの中身がおかしい場合はこれを見て
  `scrape.py` の `extract_tables` 部分を調整してください。
- GitHub Actionsのcronは「指定時刻ちょうど」ではなく数分程度前後する
  ことがあります(GitHub側の仕様)。正確な毎時0分を求める用途には
  向かない点はご了承ください。
- このデータは国土交通省が無人観測所から収集した速報値であり、
  機器の故障等による異常値が含まれる可能性がある旨、river.go.jp内でも
  注意喚起されています。重要な判断には気象庁・国交省の公式発表もあわせて
  ご確認ください。
