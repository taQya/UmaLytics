"""地方競馬データダウンロード → 日付入りファイル名で保存 → 同じディレクトリに解凍

- コマンドライン単体でも、keiba_yosou_gui.py の「地方データ取得」ボタンからでも使えます。
- requests が無い環境でも標準ライブラリ(urllib)で動くようにフォールバックします。
"""

import sys
import zipfile
from datetime import date
from pathlib import Path

try:
    import requests  # あれば使う
except ImportError:  # 無ければ標準ライブラリで代替
    requests = None
    import urllib.request

URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload"

# 保存先ディレクトリ(スクリプトと同じ場所)
BASE_DIR = Path(__file__).resolve().parent

# 解凍先ディレクトリ
EXTRACT_DIR = BASE_DIR / "horselist"

HEADERS = {
    # UAが空だと弾かれるサイトがあるのでブラウザ風にしておく
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


def _http_get(url, params, headers, timeout=60):
    """requestsがあればrequests、無ければurllibでGETしてbytesを返す"""
    if requests is not None:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    import urllib.parse
    q = urllib.parse.urlencode(params) if params else ""
    full = url + ("?" + q if q else "")
    req = urllib.request.Request(full, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(target_date: date, extract_dir: Path = None) -> Path:
    """ZIPをダウンロードして日付入りファイル名で保存する"""
    extract_dir = Path(extract_dir) if extract_dir else EXTRACT_DIR
    date_str = target_date.strftime("%Y%m%d")
    extract_dir.mkdir(parents=True, exist_ok=True)
    zip_path = extract_dir / f"race_data_{date_str}.zip"

    params = {
        "type": "daily",
        # ブラウザのNetworkタブで確認した実際のパラメータに合わせて調整してください
        # 例: "k_raceDate": target_date.strftime("%Y/%m/%d"),
    }

    print(f"ダウンロード中: {URL}")
    content = _http_get(URL, params, HEADERS, timeout=60)
    zip_path.write_bytes(content)
    print(f"保存しました: {zip_path}")
    return zip_path


def extract(zip_path: Path, extract_dir: Path = None) -> list:
    """ZIPを解凍する。解凍したファイル名のリストを返す"""
    extract_dir = Path(extract_dir) if extract_dir else EXTRACT_DIR
    if not zipfile.is_zipfile(zip_path):
        head = Path(zip_path).read_bytes()[:200]
        msg = ("ダウンロードしたファイルはZIPではありません。\n"
               "フォームページ(HTML)を取得している可能性があります。\n"
               "download_race_data.py の params(k_raceDate等)を、ブラウザの\n"
               "Networkタブで確認した実際のパラメータに合わせて調整してください。\n"
               f"先頭部分: {head.decode('utf-8', errors='replace')[:120]}")
        raise RuntimeError(msg)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
        names = zf.namelist()
        print(f"解凍しました({len(names)} ファイル):")
        for name in names:
            print(f"  - {name}")
        return names


# ============================================================
# GUI連携用
# ============================================================

def find_horselist(extract_dir: Path, target_date: date):
    """解凍先から当日の horselist CSV を探す。
    地方データは YYYYMMDD_horselist.csv 形式を想定しつつ、
    見つからなければ 'horselist' を含むCSV → 当日更新CSV の順で探索。"""
    extract_dir = Path(extract_dir)
    ymd = target_date.strftime("%Y%m%d")
    for pat in (f"*{ymd}*horselist*.csv", f"{ymd}*.csv"):
        hits = sorted(extract_dir.glob(pat))
        if hits:
            return hits[0]
    hits = sorted(extract_dir.glob("*horselist*.csv"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if hits:
        return hits[0]
    hits = sorted(extract_dir.glob("*.csv"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def fetch_nar(target_date: date = None, extract_dir: Path = None,
              progress=print):
    """ダウンロード→解凍→horselist特定 を一括で行う。
    戻り値: 見つかった horselist CSV の Path(見つからなければ None)"""
    target_date = target_date or date.today()
    extract_dir = Path(extract_dir) if extract_dir else EXTRACT_DIR
    progress(f"🌐 地方競馬データをダウンロードします({target_date:%Y-%m-%d})...")
    zip_path = download(target_date, extract_dir)
    progress(f"📦 保存: {zip_path.name}")
    names = extract(zip_path, extract_dir)
    progress(f"🗂 解凍完了({len(names)}ファイル)")
    hl = find_horselist(extract_dir, target_date)
    if hl:
        progress(f"✅ 出馬表CSVを検出: {hl.name}")
    else:
        progress("⚠ 解凍先に horselist CSV が見つかりませんでした。")
    return hl


def main() -> None:
    if len(sys.argv) > 1:
        target_date = date.fromisoformat(sys.argv[1])
    else:
        target_date = date.today()
    zip_path = download(target_date)
    try:
        extract(zip_path)
    except RuntimeError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
