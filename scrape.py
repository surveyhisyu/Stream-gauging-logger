"""
川の水位観測所ページ(www1.river.go.jp/cgi-bin/DspWaterData.exe)から
「リアルタイム10分水位一覧表」を取得し、CSVに保存するスクリプト。

このサイトは、もう一つの防災情報サイト(www.river.go.jp/kawabou/...)と違い、
JavaScriptで描画されるSPAではなく、昔ながらの「サーバー側で生成された
普通のHTML」を返すページなので、ヘッドレスブラウザ(Playwright)は使わず、
requestsで直接HTMLを取得するだけで済む(その分、実行が速く・軽い)。

ただし文字コードがUTF-8ではない(Shift_JIS系)ことが多いため、
何種類かのエンコーディングを試して、日本語として正しく読めるものを選ぶ
処理を入れている。

【重複防止・並び順・月またぎについて】
もう一つのプロジェクト(kawabou版)と同じ考え方で、
  1. スクレイプした各行について、その行の実際の日付から対象年月を判定する
  2. 対応する年月のCSVファイルを読み込み、そこにマージする
  3. (テーブル番号, 日付, 時刻) をキーに重複を排除する
     (同じ日時のデータが複数あれば、より新しく取得したものを採用)
  4. 日付・時刻が新しい順に並べ直してファイルを書き直す
という方式で保存する。このサイトの「年月日」列はフル日付
(例: 2026/09/01)なので、月をまたいでも年月を迷わず判定できる。
"""

import csv
import datetime
import os
import sys
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")

# ↓ 監視したい観測地点のリスト。今後増やす場合はここに追記する。
TARGETS = [
    {
        "name": "nishisato-10min",  # 西里橋(にしざとばし)・リアルタイム10分水位
        "url": (
            "https://www1.river.go.jp/cgi-bin/DspWaterData.exe"
            "?KIND=9&ID=304081284418020"
        ),
    },
]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")

# 日付列の見出しとして、サイトによって「日付」だったり「年月日」だったりするので
# どちらでも認識できるようにしておく
DATE_COLUMN_NAMES = ("日付", "年月日")
TIME_COLUMN_NAMES = ("時刻",)


def find_column_index(headers, candidate_names):
    """headers の中から candidate_names のいずれかに一致する列のインデックスを返す"""
    for name in candidate_names:
        if name in headers:
            return headers.index(name)
    return None


def find_header_row_index(table):
    """
    テーブルの各行を順に見て、「日付」「時刻」に該当する列が
    両方そろって見つかる最初の行を、本当の見出し行とみなしてその
    インデックスを返す。見つからなければ None を返す。

    (このサイトのテーブルは、本当の見出し行の前に
    「年月日　時刻　水位(m)」のような結合された飾りの行が
    入っていることがあるため、単純に先頭行を見出しとして
    扱うと誤動作する)
    """
    for i, row in enumerate(table):
        if find_column_index(row, DATE_COLUMN_NAMES) is not None and \
           find_column_index(row, TIME_COLUMN_NAMES) is not None:
            return i
    return None


def parse_date_ymd(date_str: str):
    """
    「2026/09/01」(YYYY/MM/DD) または「09/01」(MM/DD) の日付文字列から
    (年, 月, 日) を返す。年が分からない場合は年をNoneにする。
    """
    parts = date_str.split("/")
    if len(parts) == 3:
        return int(parts[0]), int(parts[1]), int(parts[2])
    if len(parts) == 2:
        return None, int(parts[0]), int(parts[1])
    raise ValueError(f"日付の形式が想定外です: {date_str}")


def resolve_year_month(date_str: str, now_dt: datetime.datetime):
    """
    行の日付から、保存先とすべき (年, 月) を決める。

    - フル日付(YYYY/MM/DD)なら、そのまま年月を使う(迷う余地がない)
    - 年なし(MM/DD)なら、実行時点の月と比較して「今月」か「先月」かを推測する
    """
    try:
        year, month, _day = parse_date_ymd(date_str)
    except (ValueError, AttributeError):
        return now_dt.year, now_dt.month  # パース失敗時は今月扱いにする

    if year is not None:
        return year, month

    if month == now_dt.month:
        return now_dt.year, now_dt.month
    if now_dt.month == 1:
        return now_dt.year - 1, 12
    return now_dt.year, now_dt.month - 1


def get_output_csv_path_for_month(year: int, month: int, name: str) -> str:
    """年月・地点名を指定してCSVパスを組み立てる"""
    return os.path.join(OUTPUT_DIR, name, f"{name}-{year:04d}-{month:02d}.csv")


def get_debug_html_path(name: str) -> str:
    """デバッグ用の最新HTML保存パスを地点ごとに返す"""
    return os.path.join(OUTPUT_DIR, name, f"{name}-last_page.html")


def fetch_static_html(url: str, timeout: int = 30) -> str:
    """
    通常のHTTP GETでページを取得する(JavaScript描画は不要なページ用)。

    requestsの初期設定のUser-Agent(python-requests/x.x)のままだと、
    多くのサイトで機械的なアクセスとして403 Forbiddenで拒否されるため、
    通常のブラウザからのアクセスに近いヘッダーを付けて取得する。

    文字コードがUTF-8とは限らない(むしろ日本の古い官公庁サイトは
    Shift_JIS/CP932系であることが多い)ため、いくつかの候補を順に試し、
    「水位」「観測所」などの日本語キーワードが正しく読めたものを採用する。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    raw = resp.content

    candidates = []
    if resp.apparent_encoding:
        candidates.append(resp.apparent_encoding)
    # 日本の官公庁系レガシーサイトでよくある文字コードの候補
    candidates += ["cp932", "shift_jis", "euc-jp", "utf-8"]

    tried = set()
    fallback_text = None
    for enc in candidates:
        if not enc:
            continue
        enc_lower = enc.lower()
        if enc_lower in tried:
            continue
        tried.add(enc_lower)
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if fallback_text is None:
            fallback_text = text
        if "水位" in text or "観測所" in text:
            return text

    # キーワードが見つからなくても、最初に成功したデコード結果を返す
    if fallback_text is not None:
        return fallback_text
    # すべて失敗した場合は、文字化けを許容してでも中身を返す
    return raw.decode("utf-8", errors="replace")


def extract_tables(html: str):
    """ページ内の全<table>を [ [row1cells...], [row2cells...], ... ] のリストで返す"""
    ICON_TEXT_MAP = {
        "arrow_downward": "↓",
        "arrow_upward": "↑",
        "arrow_forward": "→",
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
            # 昔ながらのHTMLサイトは、テーブルのセルの中にさらに別のテーブルが
            # 入れ子になっていることがある。tr.find_all(...)は初期設定では
            # 子孫要素すべてを再帰的に探してしまうため、何もしないと入れ子の
            # 内側テーブルのセルまで一緒に拾ってしまい、行がぐちゃぐちゃになる。
            # そのため、
            #   1. この <tr> が「今見ている table」に直接属するものだけを対象にする
            #      (入れ子の内側テーブルの<tr>は、その内側テーブルを処理する時に
            #       別途拾われるので、ここではスキップする)
            #   2. セル抽出も recursive=False にして、直接の子要素だけを見る
            # という2段階で、入れ子テーブルの混入を防ぐ。
            if tr.find_parent("table") is not table:
                continue
            cells = [
                normalize_icon_text(c.get_text(strip=True))
                for c in tr.find_all(["th", "td"], recursive=False)
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
    ファイルが存在しない場合や、ヘッダーが想定と違う場合は空dictを返す。
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

        acq_idx = header.index("取得日時") if "取得日時" in header else None
        table_idx_col = header.index("テーブル番号") if "テーブル番号" in header else None
        date_idx = find_column_index(header, DATE_COLUMN_NAMES)
        time_idx = find_column_index(header, TIME_COLUMN_NAMES)

        if None in (acq_idx, table_idx_col, date_idx, time_idx):
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
            values = [
                v for i, v in enumerate(row)
                if i not in (acq_idx, table_idx_col)
            ]
            data_date_idx = date_idx - sum(
                1 for i in (acq_idx, table_idx_col) if i < date_idx
            )
            values[data_date_idx] = last_date

            records[key] = {"取得日時": row[acq_idx], "values": values}

    return records, header


def date_time_sort_key(key):
    """(テーブル番号, 日付, 時刻) を新しい順(降順)にソートするためのキーを作る"""
    _table, date_str, time_str = key
    try:
        year, month, day = parse_date_ymd(date_str)
        hour, minute = time_str.split(":")
        return (year or 0, month, day, int(hour), int(minute))
    except (ValueError, AttributeError):
        return (0, 0, 0, 0, 0)


def process_target(target: dict, now_dt: datetime.datetime, now: str) -> None:
    """1つの観測地点(target)についてスクレイプ→保存までを行う"""
    name = target["name"]
    url = target["url"]
    label = name

    debug_html_path = get_debug_html_path(name)
    os.makedirs(os.path.dirname(debug_html_path), exist_ok=True)

    try:
        html = fetch_static_html(url)
    except Exception as e:
        print(f"[ERROR] [{label}] ページ取得に失敗しました: {e}", file=sys.stderr)
        return

    with open(debug_html_path, "w", encoding="utf-8") as f:
        f.write(html)

    tables = extract_tables(html)

    if not tables:
        print(f"[WARN] [{label}] テーブルが見つかりませんでした。{debug_html_path} を確認してください。")
        return

    # デバッグ用に、抽出できた各テーブルの中身も出力しておく
    # (想定と違う構造だった場合に、ログだけで原因を特定できるようにするため)
    for t_idx, table in enumerate(tables):
        preview = table[:3]
        print(f"[DEBUG] [{label}] table[{t_idx}] rows={len(table)} preview={preview}")

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

        header_row_idx = find_header_row_index(table)
        if header_row_idx is None:
            continue  # 日付・時刻列がそろった見出し行がない表(観測所情報などの表)はスキップ

        column_headers = table[header_row_idx]
        data_rows = table[header_row_idx + 1:]

        date_idx = find_column_index(column_headers, DATE_COLUMN_NAMES)
        time_idx = find_column_index(column_headers, TIME_COLUMN_NAMES)

        last_date = None
        table_key = str(t_idx)

        for row in data_rows:
            if len(row) <= max(date_idx, time_idx):
                continue
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
            values[date_idx] = last_date

            if key in records:
                updated_counts[bucket_key] += 1
            else:
                new_counts[bucket_key] += 1

            records[key] = {"取得日時": now, "values": values}

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
