name: Hourly river data scrape

on:
  schedule:
    # 毎時0分は世界中のワークフローが集中して遅延・スキップされやすいため、
    # 少しずらして毎時7分に実行する(GitHub公式ドキュメントでも推奨されている回避策)
    - cron: "7 * * * *"
  workflow_dispatch: {}  # 手動実行ボタンも使えるようにする

permissions:
  contents: write  # リポジトリへの自動コミットに必要

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install --with-deps chromium

      - name: Run scraper
        run: python scrape.py

      - name: Commit and push if changed
        run: |
          git config user.name "river-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data/
          git diff --quiet --cached || git commit -m "Update river data $(date -u +'%Y-%m-%d %H:%M UTC')"
          git push
