"""
川の防災情報(river.go.jp)の観測ページをヘッドレスブラウザで開き、
ページ内の表(観測値一覧)をすべて抽出してCSVに1行(1回分)ずつ追記するスクリプト。

対象URLはJavaScriptで描画されるSPAのため、requestsではなくPlaywrightで
実際にレンダリングしてからテーブルを読み取る。
"""

import csv
import datetime
import os
import sys

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ↓ 監視したいページのURL。itmkndCd/ofcCd/obsCd/fld を変えれば別地点にも流用可能
TARGET_URL = (
    "https://www.river.go.jp/kawabou/pcfull/tm"
    "?itmkndCd=4&ofcCd=21558&obsCd=1&isCurrent=true&fld=0"
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")
RAW_HTML_DEBUG = os.path.join(OUTPUT_DIR, "last_page.html")  # デバッグ用の最新HTML保存


def get_output_csv_path(now: datetime.datetime) -> str:
    """実行した年月に応じて 'data/YYYY-MM.csv' のパスを返す(月ごとにファイルを分ける)"""
    return os.path.join(OUTPUT_DIR, f"{now:%Y-%m}.csv")


def fetch_rendered_html(url: str, wait_ms: int = 8000) -> str:
    """PlaywrightでSPAを開き、レンダリング後のHTMLを返す"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
