"""
川の防災情報(river.go.jp)の観測ページをヘッドレスブラウザで開き、
ページ内の表(観測値一覧)をすべて抽出してCSVに1行(1回分)ずつ追記するスクリプト。

対象URLはJavaScriptで描画されるSPAのため、requestsではなくPlaywrightで
実際にレンダリングしてからテーブルを読み取る。

【重複防止について】
このページは毎回「直近50件程度」の観測値をまとめて返してくるため、
実行間隔がそれより短いと同じ日時のデータが何度も取得されてしまう。
そのため、CSVに書き込む前に「(テーブル番号, 日付, 時刻) の組がすでに
ファイルに存在するかどうか」をチェックし、存在する行は書き込まないようにしている。

なお、このページの表は「日付」列がグループの先頭行にしか入らず、
以降の行は空欄(横棒などではなく単に空)になる仕様になっている。
そのため、既存CSVを読み込むときも新規データを書き込むときも、
「直前に見つかった日付を引き継ぐ」処理(キャリーフォワード)が必要になる。
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
        # 実行環境(GitHub Actionsのサーバー)がUTCで動いているため、
        # 何も指定しないとページ側のJavaScriptが「今」をUTCとして判断してしまい、
        # 日本時間の最新データが「まだ来ていない未来のデータ」として
        # 切り捨てられてしまう。そのためタイムゾーンを明示的に日本時間にする。
        context = browser.new_context(timezone_id="Asia/Tokyo", locale="ja-JP")
        page = context.new_page()
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


def load_existing_keys(csv_path: str):
    """
    既存CSVを読み込み、すでに書き込み済みの (テーブル番号, 日付, 時刻) の
    組をsetで返す。日付が空欄の行は直前の日付を引き継いで解決する。

    ファイルが存在しない場合は空のsetを返す。
    """
    keys = set()
    if not os.path.isfile(csv_path):
        return keys

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return keys

        try:
            acq_idx = header.index("取得日時")
            table_idx_col = header.index("テーブル番号")
            date_idx = header.index("日付")
            time_idx = header.index("時刻")
        except ValueError:
            # ヘッダーの形式が想定と違う場合は安全側に倒して空setを返す
            # (この場合、重複チェックはできないがスクリプト自体は動く)
            print(
                "[WARN] 既存CSVのヘッダーが想定形式と異なるため、"
                "重複チェックをスキップします。",
                file=sys.stderr,
            )
            return keys

        last_block_key = None  # (取得日時, テーブル番号) が変わったらリセット
        last_date = None

        for row in reader:
            if len(row) <= max(acq_idx, table_idx_col, date_idx, time_idx):
                continue

            block_key = (row[acq_idx], row[table_idx_col])
            if block_key != last_block_key:
                # 新しい取得回・新しいテーブルの先頭に来たので日付をリセット
                last_block_key = block_key
                last_date = None

            if row[date_idx]:
                last_date = row[date_idx]

            keys.add((row[table_idx_col], last_date, row[time_idx]))

    return keys


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

    # 書き込み済みの (テーブル番号, 日付, 時刻) をあらかじめ読み込んでおく
    existing_keys = load_existing_keys(output_csv)

    file_exists = os.path.isfile(output_csv)
    written_count = 0
    skipped_count = 0

    with open(output_csv, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header_written = file_exists
        for t_idx, table in enumerate(tables):
            if not table:
                continue
            # テーブルの1行目(ページ自体の見出し: 日付/時刻/水位[m]など)を
            # そのままCSVの列名として使い、2行目以降を実データとして書き込む
            column_headers = table[0]
            # サイト上の表示順そのまま(新しい時刻が上=降順)で書き込む
            data_rows = table[1:]

            if not header_written:
                writer.writerow(["取得日時", "テーブル番号"] + column_headers)
                header_written = True

            try:
                date_idx = column_headers.index("日付")
                time_idx = column_headers.index("時刻")
            except ValueError:
                # 日付・時刻列が見つからない表は重複チェックできないので
                # そのまま全行書き込む(従来の挙動)
                date_idx = None
                time_idx = None

            last_date = None  # このテーブル内でのキャリーフォワード用
            table_key = str(t_idx)

            for row in data_rows:
                if date_idx is not None and time_idx is not None and len(row) > max(date_idx, time_idx):
                    if row[date_idx]:
                        last_date = row[date_idx]
                    time_value = row[time_idx]
                    key = (table_key, last_date, time_value)

                    if key in existing_keys:
                        skipped_count += 1
                        continue  # すでに記録済みの日時なので書き込まない

                    existing_keys.add(key)

                writer.writerow([now, t_idx] + row)
                written_count += 1

    print(
        f"[OK] {now} 時点のデータを {output_csv} に追記しました。"
        f"(テーブル数: {len(tables)}, 新規: {written_count}行, 重複スキップ: {skipped_count}行)"
    )


if __name__ == "__main__":
    main()
