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
OUTPUT_CSV = os.path.join(OUTPUT_DIR, "water_level_log.csv")
RAW_HTML_DEBUG = os.path.join(OUTPUT_DIR, "last_page.html")  # デバッグ用の最新HTML保存


def fetch_rendered_html(url: str, wait_ms: int = 8000) -> str:
    """PlaywrightでSPAを開き、レンダリング後のHTMLを返す"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        # SPAの描画完了を待つための追加待機
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
        return html


def extract_tables(html: str):
    """ページ内の全<table>を [ [row1cells...], [row2cells...], ... ] のリストで返す"""
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        html = fetch_rendered_html(TARGET_URL)
    except Exception as e:
        print(f"[ERROR] ページ取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    # デバッグ用に最新の取得結果を毎回上書き保存(中身が空だった場合の調査用)
    with open(RAW_HTML_DEBUG, "w", encoding="utf-8") as f:
        f.write(html)

    tables = extract_tables(html)

    if not tables:
        print("[WARN] テーブルが見つかりませんでした。data/last_page.html を確認してください。")
        return

    file_exists = os.path.isfile(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["取得日時", "テーブル番号", "行番号", "内容"])
        for t_idx, table in enumerate(tables):
            for r_idx, row in enumerate(table):
                writer.writerow([now, t_idx, r_idx, " | ".join(row)])

    print(f"[OK] {now} 時点のデータを {OUTPUT_CSV} に追記しました。(テーブル数: {len(tables)})")


if __name__ == "__main__":
    main()
