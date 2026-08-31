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

【重複防止・並び順・月またぎについて】
このページは毎回「直近の観測値」をまとめて返してくるため、
実行間隔がそれより短いと同じ日時のデータが何度も取得される。
また地点によっては(例: 6時間おきの観測地点)1回のスクレイプで
過去1週間以上のデータが返ってくることもあり、月をまたいだ直後は
新しい月のファイルに前の月のデータが混ざってしまう問題があった。

そこで、このスクリプトは以下の方式で保存する。
  1. スクレイプした各行について、その行の「実際の日付」から
     対象となる年月を判定する(実行時点の月と違えば前月のデータとみなす)
  2. 行ごとに、対応する年月のCSVファイル(月ごとに分かれている)を
     読み込み、そこにマージする
  3. (テーブル番号, 日付, 時刻) をキーに重複を排除する
     (同じ日時のデータが複数あれば、より新しく取得したものを採用)
  4. 日付・時刻が新しい順に並べ直してファイルを書き直す
これにより、8月31日のデータは8月のファイルに、9月1日のデータは
9月のファイルに、それぞれ正しく振り分けられる。
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


def resolve_year_month(date_str: str, now_dt: datetime.datetime):
    """
    表内の「MM/DD」形式の日付から、対象となる (年, 月) を推測する。

    観測ページは実行時点(now_dt)に近い日付のデータを返す前提で、
    - 日付の月が今月と同じなら、今年の今月として扱う
    - 違う場合は「先月」のデータとみなす(1月なら前年12月に繰り上げる)
    これにより、月をまたいだ直後の実行でも、古い月のデータは
    正しく古い月のファイルに、新しい月のデータは新しい月のファイルに
    それぞれ振り分けられる。
    """
    try:
        month = int(date_str.split("/")[0])
    except (ValueError, AttributeError, IndexError):
        return now_dt.year, now_dt.month  # パース失敗時は今月扱いにする

    if month == now_dt.month:
        return now_dt.year, now_dt.month

    # 今月と違う月 → 前月のデータとみなす
    if now_dt.month == 1:
        return now_dt.year - 1, 12
    return now_dt.year, now_dt.month - 1


def get_output_csv_path_for_month(year: int, month: int, name: str) -> str:
    """年月・地点名を指定してCSVパスを組み立てる"""
    return os.path.join(OUTPUT_DIR, name, f"{name}-{year:04d}-{month:02d}.csv")


def get_debug_html_path(name: Optional[str]) -> str:
    """デバッグ用の最新HTML保存パスを地点ごとに返す"""
    return os.path.join(OUTPUT_DIR, name, f"{name}-last_page.html")


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

    debug_html_path = get_debug_html_path(name)
    os.makedirs(os.path.dirname(debug_html_path), exist_ok=True)

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

    # スクレイプ結果を「行の実際の日付」に基づいて月ごとのバケツに振り分ける。
    # bucket_key = (year, month) -> { key: {"取得日時":..., "values":[...]} }
    buckets = {}
    bucket_headers = {}
    new_counts = {}
    updated_counts = {}

    def get_bucket(year: int, month: int):
        bucket_key = (year, month)
        if bucket_key not in buckets:
            path = get_output_csv_path_for_month(year, month, name)
            records, existing_header = load_existing_records(path)
            buckets[bucket_key] = records
            bucket_headers[bucket_key] = existing_header
            new_counts[bucket_key] = 0
            updated_counts[bucket_key] = 0
        return buckets[bucket_key]

    for t_idx, table in enumerate(tables):
        if not table:
            continue
        column_headers = table[0]
        data_rows = table[1:]

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

                year, month = resolve_year_month(last_date, now_dt)
                bucket_key = (year, month)
                records = get_bucket(year, month)
                if bucket_headers[bucket_key] is None:
                    bucket_headers[bucket_key] = ["取得日時", "テーブル番号"] + column_headers

                key = (table_key, last_date, time_value)
                values = list(row)
                values[date_idx] = last_date  # 空欄だった日付を補完

                if key in records:
                    updated_counts[bucket_key] += 1
                else:
                    new_counts[bucket_key] += 1

                # 常に最新の取得分で上書き(値が更新されている可能性に備える)
                records[key] = {"取得日時": now, "values": values}
            else:
                # 日付・時刻列がない特殊な表はキー化できないのでスキップ
                # (通常は発生しない想定)
                pass

    # バケツごとにファイルへ書き出す
    for bucket_key, records in buckets.items():
        year, month = bucket_key
        header = bucket_headers[bucket_key]
        if header is None:
            continue

        path = get_output_csv_path_for_month(year, month, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        sorted_keys = sorted(records.keys(), key=date_time_sort_key, reverse=True)

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for key in sorted_keys:
                record = records[key]
                table_key = key[0]
                writer.writerow([record["取得日時"], table_key] + record["values"])

        print(
            f"[OK] [{label}] {now} 時点のデータを {path} に反映しました。"
            f"(新規: {new_counts[bucket_key]}行, 更新: {updated_counts[bucket_key]}行, "
            f"合計: {len(records)}行)"
        )


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_dt = datetime.datetime.now(JST)
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    for target in TARGETS:
        process_target(target, now_dt, now)


if __name__ == "__main__":
    main()
