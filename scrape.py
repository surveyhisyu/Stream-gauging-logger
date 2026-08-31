"""
川の防災情報(river.go.jp)の観測ページをヘッドレスブラウザで開き、
ページ内の表(観測値一覧)をすべて抽出してCSVに保存するスクリプト。

対象URLはJavaScriptで描画されるSPAのため、requestsではなくPlaywrightで
実際にレンダリングしてからテーブルを読み取る。

【複数地点への対応について】
TARGETS リストに (名前, URL) を追加するだけで、複数の観測地点を
1回の実行でまとめて処理できる。地点ごとに保存先フォルダを分けるので、
それぞれ独立したCSVとして記録される(既存の地点は後方互換のため
これまで通り data/YYYY-MM.csv に保存され、新しく追加した地点は
data/<地点名>/YYYY-MM.csv に保存される)。

【重複防止・並び順について】
このページは毎回「直近50件程度」の観測値をまとめて返してくるため、
実行間隔がそれより短いと同じ日時のデータが何度も取得されてしまう。

また、単純にファイルの末尾に追記していくと、
「1回分のデータの中では新しい時刻が上」なのに
「実行回をまたぐと古い実行のブロックが上、新しい実行のブロックが下」
という、ねじれた並び順になってしまう。

そこで、このスクリプトは追記ではなく、
  1. 既存CSVの中身をすべて読み込む
  2. 今回スクレイプした新しいデータを合体させる
  3. (テーブル番号, 日付, 時刻) をキーに重複を排除する
     (同じ日時のデータが複数あれば、より新しく取得したものを採用)
  4. 日付・時刻が新しい順に並べ直す
  5. ファイル全体を書き直す
という方式にしている。これにより、ファイル全体が常に
「一番上が最新、下に行くほど古い」という一貫した順序になる。
"""

import csv
import datetime
import os
import sys
from typing import Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")

# ↓ 監視したい観測地点のリスト。(名前, URL) のタプルを追加すれば地点を増やせる。
#   name を指定すると、その地点は data/<name>-YYYY-MM.csv に保存される。
TARGETS = [
    {
        "name": "nishisato",  # Nishisato Bridge(既存の観測地点)
        "url": (
            "https://www.river.go.jp/kawabou/pcfull/tm"
            "?itmkndCd=4&ofcCd=21558&obsCd=1&isCurrent=true&fld=0"
        ),
    },
    {
        "name": "nakayama",  # Nakayama Bridge
        "url": (
            "https://www.river.go.jp/kawabou/pcfull/tm"
            "?itmkndCd=300&ofcCd=21000&obsCd=2100000183&isCurrent=true&fld=0"
        ),
    },
]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")


def get_output_csv_path(now: datetime.datetime, name: Optional[str]) -> str:
    """
    実行した年月・地点名に応じてCSVの保存パスを返す(月ごとにファイルを分ける)。
    'data/<name>-YYYY-MM.csv' を返す(フォルダ分けはせず data/ 直下に保存)。
    """
    return os.path.join(OUTPUT_DIR, f"{name}-{now:%Y-%m}.csv")


def get_debug_html_path(name: Optional[str]) -> str:
    """デバッグ用の最新HTML保存パスを地点ごとに返す"""
    return os.path.join(OUTPUT_DIR, f"{name}-last_page.html")


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


def load_existing_records(csv_path: str):
    """
    既存CSVを読み込み、レコードのdictを返す。

    戻り値: { (テーブル番号, 日付, 時刻): {"取得日時": ..., "values": [...]} }

    日付が空欄の行は、直前(同じ 取得日時・テーブル番号のブロック内)の
    日付を引き継いで解決する。
    ファイルが存在しない場合や、ヘッダーが想定と違う場合は空dictを返す。
    戻り値と一緒に、元のCSVの列名(header)も返す。
    """
    records = {}
    header = None

    if not os.path.isfile(csv_path):
        return records, header

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return records, None

        try:
            acq_idx = header.index("取得日時")
            table_idx_col = header.index("テーブル番号")
            date_idx = header.index("日付")
            time_idx = header.index("時刻")
        except ValueError:
            print(
                "[WARN] 既存CSVのヘッダーが想定形式と異なるため、"
                "このファイルは無視して新規作成扱いにします。",
                file=sys.stderr,
            )
            return {}, None

        last_block_key = None
        last_date = None

        for row in reader:
            if len(row) <= max(acq_idx, table_idx_col, date_idx, time_idx):
                continue

            block_key = (row[acq_idx], row[table_idx_col])
            if block_key != last_block_key:
                last_block_key = block_key
                last_date = None

            if row[date_idx]:
                last_date = row[date_idx]

            key = (row[table_idx_col], last_date, row[time_idx])
            # 「取得日時」「テーブル番号」を除いた、日付・時刻・水位などの
            # データ部分だけを values として保持する
            values = [
                v for i, v in enumerate(row)
                if i not in (acq_idx, table_idx_col)
            ]
            # values内での日付列のインデックスを再計算して補完する
            data_date_idx = date_idx - sum(
                1 for i in (acq_idx, table_idx_col) if i < date_idx
            )
            values[data_date_idx] = last_date  # 空欄だった日付を補完しておく

            records[key] = {"取得日時": row[acq_idx], "values": values}

    return records, header


def date_time_sort_key(key):
    """(テーブル番号, 日付, 時刻) を新しい順(降順)にソートするためのキーを作る"""
    _table, date_str, time_str = key
    try:
        month, day = date_str.split("/")
        hour, minute = time_str.split(":")
        return (int(month), int(day), int(hour), int(minute))
    except (ValueError, AttributeError):
        # パースできない場合は最後に回す
        return (0, 0, 0, 0)


def process_target(target: dict, now_dt: datetime.datetime, now: str) -> None:
    """1つの観測地点(target)についてスクレイプ→保存までを行う"""
    name = target["name"]
    url = target["url"]
    label = name

    output_csv = get_output_csv_path(now_dt, name)
    debug_html_path = get_debug_html_path(name)
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)

    try:
        html = fetch_rendered_html(url)
    except Exception as e:
        print(f"[ERROR] [{label}] ページ取得に失敗しました: {e}", file=sys.stderr)
        return

    # デバッグ用に最新の取得結果を毎回上書き保存(中身が空だった場合の調査用)
    with open(debug_html_path, "w", encoding="utf-8") as f:
        f.write(html)

    tables = extract_tables(html)

    if not tables:
        print(f"[WARN] [{label}] テーブルが見つかりませんでした。{debug_html_path} を確認してください。")
        return

    # 既存データを読み込む(なければ空)
    records, existing_header = load_existing_records(output_csv)

    new_count = 0
    updated_count = 0
    header = existing_header

    for t_idx, table in enumerate(tables):
        if not table:
            continue
        column_headers = table[0]
        data_rows = table[1:]

        if header is None:
            header = ["取得日時", "テーブル番号"] + column_headers

        try:
            date_idx = column_headers.index("日付")
            time_idx = column_headers.index("時刻")
        except ValueError:
            date_idx = None
            time_idx = None

        last_date = None
        table_key = str(t_idx)

        for row in data_rows:
            if date_idx is not None and time_idx is not None and len(row) > max(date_idx, time_idx):
                if row[date_idx]:
                    last_date = row[date_idx]
                time_value = row[time_idx]
                key = (table_key, last_date, time_value)

                values = list(row)
                values[date_idx] = last_date  # 空欄だった日付を補完

                if key in records:
                    updated_count += 1
                else:
                    new_count += 1

                # 常に最新の取得分で上書き(値が更新されている可能性に備える)
                records[key] = {"取得日時": now, "values": values}
            else:
                # 日付・時刻列がない特殊な表はキー化できないのでそのまま追加のみ扱い
                # (通常は発生しない想定)
                pass

    if header is None:
        print(f"[WARN] [{label}] ヘッダー情報を決定できませんでした。")
        return

    # 新しい時刻が上に来るように並べ替え
    sorted_keys = sorted(records.keys(), key=date_time_sort_key, reverse=True)

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for key in sorted_keys:
            record = records[key]
            table_key = key[0]
            writer.writerow([record["取得日時"], table_key] + record["values"])

    print(
        f"[OK] [{label}] {now} 時点のデータを {output_csv} に反映しました。"
        f"(新規: {new_count}行, 更新: {updated_count}行, 合計: {len(records)}行)"
    )


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_dt = datetime.datetime.now(JST)
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    for target in TARGETS:
        process_target(target, now_dt, now)


if __name__ == "__main__":
    main()
