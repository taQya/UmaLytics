#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地方競馬(keiba.go.jp)の馬データベース化
=====================================================================
deba_table.py の出馬表ページは「直近5走」しか載っていない。keiba.go.jp には
馬ごとに**生涯の全レース履歴**と**血統・生産者・馬主などのプロフィール**を
1頭1URLで見られるページがあり、ここから取得してSQLiteに蓄積する。

  馬柱(HorseMarkInfo)  : 生涯の全レース(日付・競馬場・着順・タイム・着差・
                         上がり3F・馬体重・騎手・斤量・調教師・収得賞金 等)
  馬情報(RaceHorseInfo): 生年月日・毛色・馬主・生産者・産地・3代血統・
                         調教師所属・収得賞金累計・現役/引退

勝率や距離別成績などの「集計値」はレース履歴から自分で計算できるので二重に
持たない(races テーブルにSQLで問い合わせれば出る)。プロフィール側は集計値
テーブルもパースするが、参考値として profile_json にまとめて保持するだけ。

使い方:
    # 1) 血統登録番号を集める(通信なし。deba_table.py のキャッシュから収集)
    python horse_db.py --seed horselist/deba_*.json

    # 2) 未取得(または古い)馬のデータを取得してDBに追加
    python horse_db.py --fetch
    python horse_db.py --fetch --limit 200      # 様子見に件数を絞る

DBファイルは同じフォルダの horses.sqlite3。1頭につき2リクエスト、1秒間隔
なので、初回は馬数に応じて数時間かかる(例: 7000頭で約4時間)。既に新鮮な
データがある馬はスキップするので、2回目以降はほぼ差分だけで済む。
"""

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

from deba_table import HEADERS, SLEEP, _get, _sec, _text  # noqa: F401 (HEADERSはHTTP共通化のため)

DB_PATH = Path(__file__).resolve().parent / "horses.sqlite3"
MARK_URL = "https://www.keiba.go.jp/KeibaWeb/DataRoom/HorseMarkInfo"
INFO_URL = "https://www.keiba.go.jp/KeibaWeb/DataRoom/RaceHorseInfo"
FRESH_DAYS = 7   # このデータより新しければ再取得しない(引退馬は増えないので実質無限)

SCHEMA = """
CREATE TABLE IF NOT EXISTS horses (
    lineage_code   TEXT PRIMARY KEY,
    name           TEXT,
    sex            TEXT,
    discipline     TEXT,          -- （日輓）＝ばんえい 等
    status         TEXT,          -- 現役/引退 等
    birth_date     TEXT,
    coat_color     TEXT,
    trainer        TEXT,
    trainer_affili TEXT,
    owner          TEXT,
    birthplace     TEXT,
    breeder        TEXT,
    sire           TEXT,
    sire_sire      TEXT,
    sire_dam       TEXT,
    dam            TEXT,
    dam_sire       TEXT,
    dam_dam        TEXT,
    earnings_local   INTEGER,
    earnings_central INTEGER,
    earnings_central_bonus INTEGER,
    profile_json   TEXT,          -- 集計テーブル(生涯/地方/中央/距離別)の生データ
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS races (
    lineage_code TEXT,
    race_date    TEXT,
    track        TEXT,
    race_no      INTEGER,
    race_name    TEXT,
    grade        TEXT,
    distance     INTEGER,
    weather      TEXT,
    track_cond   TEXT,
    n_horses     INTEGER,
    waku         INTEGER,
    umaban       INTEGER,
    ninki        INTEGER,
    chaku        INTEGER,
    chaku_status TEXT,            -- 中止/除外/取消 等(着順が数字でない場合)
    time_sec     REAL,
    margin       REAL,
    agari3f      REAL,
    weight       INTEGER,
    jockey       TEXT,
    jockey_affili TEXT,
    kinryo       REAL,
    trainer      TEXT,
    prize        INTEGER,
    rival        TEXT,            -- 1着なら2着馬、それ以外は1着馬
    PRIMARY KEY (lineage_code, race_date, race_no)
);
CREATE INDEX IF NOT EXISTS idx_races_horse ON races(lineage_code);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);
CREATE INDEX IF NOT EXISTS idx_races_horse_date ON races(lineage_code, race_date);
CREATE INDEX IF NOT EXISTS idx_horses_name ON horses(name);
"""


def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


# ============================================================
# 血統登録番号の収集(通信なし)
# ============================================================

def codes_from_cache(patterns):
    """deba_table.py のキャッシュ(deba_*.json)から {血統登録番号: 馬名} を集める(通信なし)"""
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    codes = {}
    for f in files:
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for race in data.values():
            for h in race.values():
                code = h.get("血統登録番号")
                if code:
                    codes[code] = h.get("馬名")
    return codes


def seed_from_deba_cache(patterns, con):
    """codes_from_cache() の結果をDBに登録する。
    既存のプロフィール行は上書きしない(name列だけ埋まった仮登録として扱う)。"""
    codes = codes_from_cache(patterns)
    cur = con.cursor()
    added = 0
    for code, name in codes.items():
        cur.execute(
            "INSERT INTO horses (lineage_code, name) VALUES (?, ?) "
            "ON CONFLICT(lineage_code) DO UPDATE SET "
            "name = COALESCE(horses.name, excluded.name)",
            (code, name))
        added += cur.rowcount
    con.commit()
    return len(codes)


# ============================================================
# 馬柱(生涯レース履歴)のパース
# ============================================================

_RACE_COLS = (
    "date", "track", "race_no", "race_name", "grade", "distance", "weather",
    "track_cond", "_blank", "n_horses", "waku", "umaban", "ninki", "chaku",
    "time", "margin", "agari3f", "weight", "jockey", "kinryo", "trainer",
    "prize", "rival",
)


def parse_race_log(html):
    """HorseMarkInfo のHTML → レース辞書のリスト(新しい順)"""
    i, j = html.find("<tbody>"), html.find("</tbody>")
    if i < 0 or j < 0:
        return []
    races = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html[i:j], re.S):
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        if len(tds) != len(_RACE_COLS):
            continue
        cell = {name: _text(raw) for name, raw in zip(_RACE_COLS, tds)}

        chaku_txt = cell["chaku"]
        m = re.match(r"20\d\d/\d\d/\d\d", cell["date"])
        jockey_m = re.match(r"(\S+?)\s*\(([^)]*)\)\s*$", cell["jockey"])

        races.append({
            "race_date": cell["date"].replace("/", "-") if m else None,
            "track": cell["track"] or None,
            "race_no": int(cell["race_no"]) if cell["race_no"].isdigit() else None,
            "race_name": cell["race_name"] or None,
            "grade": cell["grade"] or None,
            "distance": int(cell["distance"]) if cell["distance"].isdigit() else None,
            "weather": cell["weather"] or None,
            "track_cond": cell["track_cond"] or None,
            "n_horses": int(cell["n_horses"]) if cell["n_horses"].isdigit() else None,
            "waku": int(cell["waku"]) if cell["waku"].isdigit() else None,
            "umaban": int(cell["umaban"]) if cell["umaban"].isdigit() else None,
            "ninki": int(cell["ninki"]) if cell["ninki"].isdigit() else None,
            "chaku": int(chaku_txt) if chaku_txt.isdigit() else None,
            "chaku_status": None if chaku_txt.isdigit() else (chaku_txt or None),
            "time_sec": _sec(cell["time"]),
            "margin": float(cell["margin"]) if re.fullmatch(r"-?\d+(\.\d+)?", cell["margin"]) else None,
            "agari3f": float(cell["agari3f"]) if re.fullmatch(r"\d+(\.\d+)?", cell["agari3f"]) else None,
            "weight": int(cell["weight"]) if cell["weight"].isdigit() else None,
            "jockey": jockey_m.group(1) if jockey_m else (cell["jockey"] or None),
            "jockey_affili": jockey_m.group(2) if jockey_m else None,
            "kinryo": float(cell["kinryo"]) if re.fullmatch(r"\d+(\.\d+)?", cell["kinryo"]) else None,
            "trainer": cell["trainer"] or None,
            "prize": int(cell["prize"].replace(",", "")) if re.fullmatch(r"[\d,]+", cell["prize"]) else None,
            "rival": cell["rival"].strip("（）() ") or None,
        })
    return races


# ============================================================
# 馬情報(プロフィール・血統)のパース
# ============================================================

def _label_value_pairs(table_html):
    """<td class="intablelabel...">ラベル</td><td>値</td> の並びを辞書にする"""
    out = {}
    tds = re.findall(r'<td\b([^>]*)>(.*?)</td>', table_html, re.S)
    i = 0
    while i < len(tds) - 1:
        attr, content = tds[i]
        if "intablelabel" in attr:
            out[_text(content)] = _text(tds[i + 1][1])
            i += 2
        else:
            i += 1
    return out


def parse_profile(html):
    """RaceHorseInfo のHTML → プロフィールdict(取得できなければ空dict)"""
    m = re.search(r'<ul class="horseinfo horse">(.*?)</ul>', html, re.S)
    header = m.group(1) if m else ""
    lis = re.findall(r"<li\b[^>]*>(.*?)</li>", header, re.S)
    name = _text(lis[0]) if len(lis) > 0 else None       # 馬名(h4)
    sex_age = _text(lis[1]) if len(lis) > 1 else ""       # 例: "牝3"
    status = _text(lis[2]) if len(lis) > 2 else None      # 現役/引退 等
    discipline = _text(lis[3]) if len(lis) > 3 else None  # （日輓）等
    m = re.match(r"(\D+)", sex_age)
    sex = m.group(1).strip() if m else None

    m = re.search(r'<table class="horse_info_table">.*?</table>', html, re.S)
    info = _label_value_pairs(m.group(0)) if m else {}

    def money(label):
        v = re.sub(r"[^\d]", "", info.get(label, ""))
        return int(v) if v else None

    trainer_m = re.match(r"(\S+?)\s*(?:（(.*?)）)?\s*$",
                         re.sub(r"[　\s]+", "", info.get("調教師", "")))

    pedigree = {}
    m = re.search(r'<table>\s*<thead>\s*<tr>\s*<th colspan="4">血統表</th>.*?</table>', html, re.S)
    if m:
        cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', m.group(0), re.S)
        cur_label = None
        for attr, content in cells:
            txt = _text(content)
            if attr.strip() in ('class="blue" rowspan="2"', 'class="pink" rowspan="2"',
                                'class="blue"', 'class="pink"'):
                cur_label = txt
            elif cur_label:
                pedigree[cur_label] = txt
                cur_label = None

    profile_tables = re.findall(r'<table>\s*<thead>\s*<tr>\s*<th class="wide">.*?</table>', html, re.S)

    return {
        "name": name, "sex": sex,
        "status": status, "discipline": discipline,
        "birth_date": (info.get("生年月日") or "").rstrip("生") or None,
        "coat_color": info.get("毛色") or None,
        "trainer": trainer_m.group(1) if trainer_m else None,
        "trainer_affili": trainer_m.group(2) if trainer_m else None,
        "owner": re.sub(r"[　\s]+", "", info.get("馬主", "")) or None,
        "birthplace": info.get("産地") or None,
        "breeder": re.sub(r"[　\s]+", "", info.get("生産牧場", "")) or None,
        "sire": pedigree.get("父"), "sire_sire": pedigree.get("父父"),
        "sire_dam": pedigree.get("父母"), "dam": pedigree.get("母"),
        "dam_sire": pedigree.get("母父"), "dam_dam": pedigree.get("母母"),
        "earnings_local": money("地方収得賞金"),
        "earnings_central": money("中央収得賞金"),
        "earnings_central_bonus": money("中央付加賞金"),
        "profile_json": json.dumps(
            [re.sub(r"\s+", " ", t) for t in profile_tables], ensure_ascii=False) or None,
    }


# ============================================================
# 取得(HTTP) & DB更新
# ============================================================

def fetch_horse(code):
    """1頭ぶんの馬柱+馬情報を取得して {profile}, [races] を返す"""
    log_html = _get(f"{MARK_URL}?k_lineageLoginCode={code}")
    time.sleep(SLEEP)
    info_html = _get(f"{INFO_URL}?k_lineageLoginCode={code}&k_activeCode=1")
    return parse_profile(info_html), parse_race_log(log_html)


def upsert_horse(con, code, profile, races):
    cur = con.cursor()
    cols = ["name", "sex", "discipline", "status", "birth_date", "coat_color",
            "trainer", "trainer_affili", "owner", "birthplace", "breeder",
            "sire", "sire_sire", "sire_dam", "dam", "dam_sire", "dam_dam",
            "earnings_local", "earnings_central", "earnings_central_bonus",
            "profile_json"]
    cur.execute(
        f"INSERT INTO horses (lineage_code, {', '.join(cols)}, updated_at) "
        f"VALUES (?, {', '.join('?' * len(cols))}, ?) "
        f"ON CONFLICT(lineage_code) DO UPDATE SET "
        f"{', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=excluded.updated_at",
        (code, *[profile.get(c) for c in cols], date.today().isoformat()))

    rcols = ["track", "race_no", "race_name", "grade", "distance", "weather",
             "track_cond", "n_horses", "waku", "umaban", "ninki", "chaku",
             "chaku_status", "time_sec", "margin", "agari3f", "weight",
             "jockey", "jockey_affili", "kinryo", "trainer", "prize", "rival"]
    for r in races:
        if not r["race_date"]:
            continue
        cur.execute(
            f"INSERT INTO races (lineage_code, race_date, {', '.join(rcols)}) "
            f"VALUES (?, ?, {', '.join('?' * len(rcols))}) "
            f"ON CONFLICT(lineage_code, race_date, race_no) DO UPDATE SET "
            f"{', '.join(f'{c}=excluded.{c}' for c in rcols)}",
            (code, r["race_date"], *[r[c] for c in rcols]))
    con.commit()


def stale_codes(con, fresh_days=FRESH_DAYS):
    cutoff = date.today().isoformat()
    cur = con.execute(
        "SELECT lineage_code FROM horses WHERE updated_at IS NULL "
        "OR julianday(?) - julianday(updated_at) >= ? "
        "ORDER BY updated_at IS NOT NULL, lineage_code", (cutoff, fresh_days))
    return [r[0] for r in cur.fetchall()]


def fetch_codes(con, codes, progress=print):
    """指定した血統登録番号を無条件で(新鮮さを見ずに)取得・upsertする"""
    if not codes:
        return 0
    secs = int(len(codes) * SLEEP * 2)
    progress(f"🌐 {len(codes)}頭のデータを取得します(約{secs // 3600}時間{secs % 3600 // 60}分)...")
    got = 0
    for i, code in enumerate(codes):
        try:
            profile, races = fetch_horse(code)
        except Exception as e:
            progress(f"  ⚠ {code} 取得失敗: {e}")
            time.sleep(SLEEP)
            continue
        upsert_horse(con, code, profile, races)
        got += 1
        if (i + 1) % 25 == 0:
            progress(f"  ... {i + 1}/{len(codes)}頭 "
                     f"(直近: {profile.get('name') or code})")
        time.sleep(SLEEP)
    progress(f"✅ {got}/{len(codes)}頭を更新しました")
    return got


def update_db(con, limit=None, progress=print):
    """DB全体を掃引し、7日以上古い(または未取得の)馬をまとめて取得する。
    毎日回すと同じ日にまとめて再取得されがちなので、日次更新には daily_update() を使う。"""
    codes = stale_codes(con)
    if limit:
        codes = codes[:limit]
    if not codes:
        progress("✅ 更新が必要な馬はありません(全馬が新鮮なデータを持っています)")
        return 0
    return fetch_codes(con, codes, progress)


def daily_update(db_path=DB_PATH, target_date=None, progress=print):
    """その日の地方競馬データを取得し、出走した馬(+初見の馬)だけをDBに反映する。
    毎日決まった時刻に実行する用(1日あたり数百頭・数十分程度で終わる想定)。
    戻り値: (取得成功頭数, 対象頭数)"""
    import download_race_data as ddl

    hl = ddl.fetch_nar(target_date, progress=progress)
    if hl is None:
        progress("⚠ 本日の地方競馬データが見つからないため、馬DB更新をスキップします。")
        return 0, 0

    import deba_table as dt
    import keiba_yosou as eng
    horses = eng.load_horselist(hl)
    races = {(h.place, h.race_no) for h in horses if h.place in dt.BABA_CODES}
    if not races:
        progress("⚠ 対象の地方競馬レースが無いため、馬DB更新をスキップします。")
        return 0, 0

    dt.fetch_day(hl, races, progress=progress)

    cache_file = dt.cache_file(hl)
    codes = list(codes_from_cache([str(cache_file)]))
    con = connect(db_path)
    added = seed_from_deba_cache([str(cache_file)], con)
    progress(f"📋 本日の出走馬 {len(codes)}頭(新規{added}頭)をDBに反映します")
    got = fetch_codes(con, codes, progress)
    con.close()
    return got, len(codes)


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="地方競馬の馬データベースを構築・更新する")
    ap.add_argument("--seed", nargs="*", metavar="PATTERN",
                    help="deba_table.py のキャッシュ(deba_*.json)から血統登録番号を収集"
                         "(省略時: horselist/deba_*.json)")
    ap.add_argument("--fetch", action="store_true", help="未取得/古い馬のデータを取得")
    ap.add_argument("--daily", action="store_true",
                    help="本日の地方競馬データを取得し、出走馬(+初見の馬)だけをDBに反映する"
                         "(毎日のスケジュール実行向け。run_horse_db_daily.bat 参照)")
    ap.add_argument("--limit", type=int, default=None, help="--fetch で取得する頭数の上限")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="DBファイルのパス")
    args = ap.parse_args()

    if args.daily:
        connect(args.db).close()  # スキーマだけ先に用意しておく
        daily_update(args.db)
        return

    con = connect(args.db)
    if args.seed is not None:
        patterns = args.seed or ["horselist/deba_*.json"]
        n = seed_from_deba_cache(patterns, con)
        print(f"📋 血統登録番号 {n}件をキャッシュから収集しました")
    if args.fetch:
        update_db(con, args.limit)
    if not args.seed and not args.fetch:
        n_horses = con.execute("SELECT COUNT(*) FROM horses").fetchone()[0]
        n_races = con.execute("SELECT COUNT(*) FROM races").fetchone()[0]
        n_done = con.execute(
            "SELECT COUNT(*) FROM horses WHERE updated_at IS NOT NULL").fetchone()[0]
        print(f"📊 {args.db}: 馬{n_horses}頭(取得済み{n_done}) / レース履歴{n_races}件")
        print("   --seed で血統登録番号を収集、--fetch でデータ取得できます")
    con.close()


if __name__ == "__main__":
    main()
