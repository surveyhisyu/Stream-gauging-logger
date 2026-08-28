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
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")

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
        page.goto(url, wait_until="networkidle", timeout=60000)
        # SPAの描画完了を待つための追加待機
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
        return html


def extract_tables(html: str):
    """ページ内の全<table>を [ [row1cells...], [row2cells...], ... ] のリストで返す"""
    # サイト上では矢印アイコンで表示されている部分が、テキスト抽出すると
    # "arrow_downward" のようなアイコン名の文字列になってしまうため、
    # 見やすい矢印記号に変換する
    ICON_TEXT_MAP = {
        "arrow_downward": "↓",  # 水位低下
        "arrow_upward": "↑",    # 水位上昇
        "arrow_forward": "→",   # 横ばい
        "arrow_back": "←",
    }

    def normalize_icon_text(text: str) -> str:
        for name, symbol in ICON_TEXT_MAP.items():
            if name in text:
                text = text.replace(name, f" {symbol}")
        return text.strip()

    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [
                normalize_icon_text(c.get_text(strip=True))
                for c in tr.find_all(["th", "td"])
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_dt = datetime.datetime.now(JST)
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    output_csv = get_output_csv_path(now_dt)

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

    file_exists = os.path.isfile(output_csv)
    with open(output_csv, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header_written = file_exists
        for t_idx, table in enumerate(tables):
            if not table:
                continue
            # テーブルの1行目(ページ自体の見出し: 日付/時刻/水位[m]など)を
            # そのままCSVの列名として使い、2行目以降を実データとして書き込む
            column_headers = table[0]
            # サイト上は新しい時刻が上に表示される(降順)ため、
            # 古い→新しいの昇順になるよう反転させる
            data_rows = list(reversed(table[1:]))

            if not header_written:
                writer.writerow(["取得日時", "テーブル番号"] + column_headers)
                header_written = True

            for row in data_rows:
                writer.writerow([now, t_idx] + row)

    print(f"[OK] {now} 時点のデータを {output_csv} に追記しました。(テーブル数: {len(tables)})")


if __name__ == "__main__":
    main()
