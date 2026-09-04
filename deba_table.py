#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出馬表ページ(keiba.go.jp)から、horselist CSV に無い馬情報を収集する
=====================================================================
horselist CSV(地方)には前走以前の情報が一切入っていないため、予想エンジンの
近走評価が地方競馬では機能していなかった。出馬表ページには以下が載っている。

  過去5走ぶん: 着順 / 日付 / 馬場 / 頭数 / 競馬場 / 回り / 距離 / 馬番 /
               レース名 / 人気 / 馬体重 / 騎手 / 斤量 /
               走破タイム / コーナー通過順 / 上がり3F / 着差 / 勝ち馬
  当日ぶん:     枠番 / 単勝オッズ / 人気 / 馬体重 / 最高タイム

ここから「近走点」「着差」「上がり」「脚質(コーナー通過順)」「レース間隔」
「継続騎乗」の6特徴量を作り、予想スコアに加算する。重みは PARAMS 経由で
tune_params.py から調整できる。

使い方(取得):
    python deba_table.py horselist/20260810_horselist.csv     # 1日分
    python deba_table.py horselist/*_horselist.csv            # まとめて過去分も
取得結果は horselist と同じフォルダの deba_YYYYMMDD.json にキャッシュされ、
load_horselist() から自動で読み込まれる(オフラインでも予想できる)。
"""

import json
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

import race_grade

BASE = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTable"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SLEEP = 10.0         # リクエスト間隔(秒)。keiba.go.jp の robots.txt が Crawl-delay: 10 を
                     # 指定しているため、それに合わせている(このモジュールが使う
                     # /KeibaWeb/TodayRaceInfo/ 自体はrobots.txtでは全bot対象にDisallow
                     # 指定されている。個人利用の範囲での手動〜低頻度実行にとどめること)
PAST_COLS = 5        # 出馬表に載る過去走の数(前走〜5走前)

# 競馬場名 → babaCode。月別開催日程ページ(MonthlyConveneInfo)の対応表。
BABA_CODES = {
    "帯広ば": 3, "門別": 36, "盛岡": 10, "水沢": 11, "浦和": 18, "船橋": 19,
    "大井": 20, "川崎": 21, "金沢": 22, "笠松": 23, "名古屋": 24, "園田": 27,
    "姫路": 28, "高知": 31, "佐賀": 32,
}


# ============================================================
# HTML パース
# ============================================================

def _text(html_frag):
    """タグを除去して正規化した文字列を返す"""
    s = re.sub(r"<[^>]+>", " ", html_frag)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">").replace("　", " "))
    return re.sub(r"\s+", " ", s).strip()


def _sec(s):
    """'1:17.6' → 77.6 / '17.3' → 17.3。取れなければ None"""
    m = re.search(r"(?:(\d+):)?(\d+\.\d+)", str(s))
    if not m:
        return None
    return (int(m.group(1)) * 60 if m.group(1) else 0) + float(m.group(2))


def _rows(block):
    """horse ブロックを行(セルのリスト)に分解する。
    着別成績の入れ子テーブルは先に取り除く(取り除いた中身も返す)。"""
    nested = re.findall(r"<table\b.*?</table>", block, re.S)
    flat = re.sub(r"<table\b.*?</table>", "", block, flags=re.S)
    # 入れ子テーブルを外してから、この馬のブロック末尾(表の終わり)で切る
    flat = re.split(r"</tbody>|</table>", flat)[0]
    rows = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", flat, re.S):
        cells = [c for _, c in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", row, re.S)]
        if cells:
            rows.append(cells)
    # 先頭行は <tr class="tBorder"> で split 済みなので </tr> が無く拾えない
    head = flat.split("<tr", 1)[0]
    head_cells = [c for _, c in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", head, re.S)]
    if head_cells:
        rows.insert(0, head_cells)
    return rows, nested


def _parse_past(cells_by_row):
    """各行の末尾5セルを列方向に組み替えて過去5走を作る"""
    cols = []
    for i in range(PAST_COLS):
        col = {}
        for name, cells in cells_by_row.items():
            tail = cells[-PAST_COLS:] if len(cells) >= PAST_COLS else []
            col[name] = tail[i] if len(tail) == PAST_COLS else ""
        cols.append(col)

    past = []
    for col in cols:
        info = col.get("info", "")
        if "raceInfo" not in info:
            break
        m = re.search(r'class="pastRank[^"]*">(.*?)</span>', info, re.S)
        rank_raw = _text(m.group(1)) if m else ""
        body = _text(re.sub(r'<span class="pastRank.*?</span>', "", info, flags=re.S))
        # 例: '26.07.28 稍重 9頭 盛岡 左1200 1番' / '26.07.26 1.6 10頭 帯広 直200 5番'
        #     '26.07.26 重 11頭 盛岡 芝左1600 3番' (芝コースは種別が付く)
        m = re.match(r"(\d\d)\.(\d\d)\.(\d\d)\s+(\S+)\s+(\d+)頭\s+(\S+)\s+"
                     r"(芝|ダ)?([左右直])(\d+)\s+(\d+)番", body)
        if not m:
            break
        run = {
            "着順": int(rank_raw) if rank_raw.isdigit() else None,
            "状態": None if rank_raw.isdigit() else (rank_raw or None),
            "日付": f"20{m.group(1)}-{m.group(2)}-{m.group(3)}",
            "馬場": m.group(4), "頭数": int(m.group(5)), "競馬場": m.group(6),
            "芝ダート": m.group(7) or "ダ", "回り": m.group(8),
            "距離": int(m.group(9)), "馬番": int(m.group(10)),
            "レース名": _text(col.get("name", "")) or None,
        }
        # '7人 485 鈴木祐 55.0' (人気・馬体重・騎手・斤量)
        m = re.match(r"(\d+)人\s+(\d+)\s+(\S+?)\s+([\d.]+)$", _text(col.get("jockey", "")))
        if m:
            run.update({"人気": int(m.group(1)), "馬体重": int(m.group(2)),
                        "騎手": m.group(3), "斤量": float(m.group(4))})
        # '1:17.6 6-6 39.8' (走破タイム・コーナー通過順・上がり3F)
        t = _text(col.get("time", ""))
        run["タイム"] = _sec(t)
        m = re.search(r"(\d+(?:-\d+)+)", t)
        run["通過順"] = [int(x) for x in m.group(1).split("-")] if m else None
        m = re.search(r"(\d+\.\d)\s*$", t)
        run["上がり3F"] = float(m.group(1)) if m and run["通過順"] else None
        # '2.5 フレンドヘネシー' (着差・勝ち馬。1着なら2着馬との差)
        d = _text(col.get("margin", ""))
        m = re.match(r"([\d.]+)?\s*(\S+)?$", d)
        if m:
            run["着差"] = float(m.group(1)) if m.group(1) else None
            run["相手"] = m.group(2)
        past.append(run)
    return past


def parse_deba(html):
    """出馬表ページのHTML → {馬番: 馬情報dict}"""
    blocks = re.split(r'<tr class="tBorder">', html)[1:]
    horses = {}
    for block in blocks:
        rows, nested = _rows(block)
        if len(rows) < 5:
            continue
        r1, r2, r3, r4, r5 = rows[:5]
        m = re.search(r'class="horseNum"[^>]*>\s*(\d+)', block)
        if not m:
            continue
        umaban = int(m.group(1))

        h = {"馬番": umaban}
        m = re.search(r'class="courseNum[^"]*"[^>]*>\s*(\d+)', block)
        h["枠番"] = int(m.group(1)) if m else None
        m = re.search(r'k_lineageLoginCode=(\d+)"[^>]*>(.*?)</a>', block, re.S)
        if m:
            h["血統登録番号"], h["馬名"] = m.group(1), _text(m.group(2))
        m = re.search(r'class="jockeyName"[^>]*>(.*?)</a>', block, re.S)
        h["騎手"] = re.sub(r"（.*?）", "", _text(m.group(1))).strip() if m else None
        m = re.search(r'class="odds_Black[^"]*">\s*([\d.]+)', block)
        h["オッズ"] = float(m.group(1)) if m else None
        m = re.search(r"\((\d+)人気\)", block)
        h["人気"] = int(m.group(1)) if m else None
        m = re.search(r'class="odds_weight"[^>]*>\s*(\d+)\s*<br>\s*\(([-+]?\d+)\)', block, re.S)
        if m:
            h["馬体重"], h["馬体重増減"] = int(m.group(1)), int(m.group(2))
        if nested:  # 着別成績テーブルの最終行に最高タイムと馬場
            tail = re.findall(r"<td[^>]*>(.*?)</td>", nested[0].rsplit("<tr", 1)[-1], re.S)
            if len(tail) >= 2:
                h["最高タイム"] = _sec(_text(tail[0]))
                h["最高タイム馬場"] = _text(tail[1]).rstrip("－-") or None

        h["過去走"] = _parse_past(
            {"info": r1, "name": r2, "jockey": r3, "time": r4, "margin": r5})
        horses[umaban] = h
    return horses


# ============================================================
# 取得(HTTP)
# ============================================================

def _get(url, timeout=30):
    if requests is not None:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_race(ymd, place, race_no):
    """1レース分の出馬表を取得して {馬番: 馬情報} を返す"""
    code = BABA_CODES.get(place)
    if code is None:
        raise ValueError(f"babaCode不明の競馬場: {place}")
    date = f"{ymd[:4]}%2F{ymd[4:6]}%2F{ymd[6:8]}"
    url = f"{BASE}?k_raceDate={date}&k_babaCode={code}&k_raceNo={race_no}"
    return parse_deba(_get(url))


# ============================================================
# キャッシュ(JSON)
# ============================================================

def cache_file(horselist_path):
    hp = Path(horselist_path)
    m = re.search(r"(20\d{6})", hp.name.replace("_", ""))
    return hp.parent / f"deba_{m.group(1) if m else 'today'}.json"


def load_cache(horselist_path):
    f = cache_file(horselist_path)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_day(horselist_path, races, progress=print, refresh=False):
    """未取得のレースだけ出馬表を取得してキャッシュに追記する。
    races: [(競馬場, レース番号), ...]  戻り値: (取得数, 全レース数)"""
    hp = Path(horselist_path)
    m = re.search(r"(20\d{6})", hp.name.replace("_", ""))
    if not m:
        raise ValueError(f"ファイル名から開催日を判別できません: {hp.name}")
    ymd = m.group(1)
    cache = {} if refresh else load_cache(hp)
    todo = [(p, r) for p, r in sorted(races) if refresh or f"{p}|{r}" not in cache]
    if not todo:
        progress(f"✅ {ymd}: 取得済み({len(cache)}レース)")
        return 0, len(cache)

    progress(f"🌐 {ymd}: 出馬表を{len(todo)}レース取得します"
             f"(約{int(len(todo) * SLEEP)}秒)...")
    got = 0
    for i, (place, race_no) in enumerate(todo):
        if place not in BABA_CODES:
            progress(f"  ⚠ 競馬場コード不明のためスキップ: {place}")
            continue
        try:
            data = fetch_race(ymd, place, race_no)
        except Exception as e:
            progress(f"  ⚠ {place}{race_no}R 取得失敗: {e}")
            continue
        if data:
            cache[f"{place}|{race_no}"] = data
            got += 1
        if i < len(todo) - 1:
            time.sleep(SLEEP)
        if (i + 1) % 10 == 0:
            progress(f"  ... {i + 1}/{len(todo)}レース")
    cache_file(hp).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    progress(f"✅ {ymd}: {got}レース取得(合計{len(cache)}レース)")
    return got, len(cache)


def attach(horses, horselist_path):
    """キャッシュ済みの出馬表データを Horse.deba に載せる。
    通信は行わないので、キャッシュが無ければ何もしない。戻り値: 紐付いた頭数"""
    cache = load_cache(horselist_path)
    if not cache:
        return 0
    n = 0
    for h in horses:
        race = cache.get(f"{h.place}|{h.race_no}")
        if not race:
            continue
        d = race.get(str(h.umaban))
        if d and d.get("馬名") in ("", None, h.name):
            h.deba = d
            n += 1
    return n


# ============================================================
# 特徴量とスコアリング
# ============================================================
# 出馬表由来の重みパラメータ。エンジン側の PARAMS にこの初期値が入る。
#
# 【検証結果に基づく控えめな初期値】
# 勝ち馬と負け馬を単純平均で比べると、近走点・着差・上がり3F・脚質のいずれも
# 明確な差がある(=特徴量自体は有効)。しかし手持ちデータ(22日・921レース)で
# 時系列train/test分割してチューニングすると、学習データ側の改善(◎的中+3)が
# テストデータ側では再現しなかった(過学習)。既存パラメータ(total_win等)が
# 既にこのデータに強くチューニング済みで、上乗せの余地を正確に測るには
# データ量がまだ足りていない。そのため初期値は「悪化させない」範囲の小さな
# 値にとどめ、複数週分のデータが貯まってから tune_params.py で調整することを
# 前提にしている。
PARAMS_DEFAULT = {
    "deba_recent": 0.5,    # 過去5走の着順点(-0.6〜2.0)
    "deba_margin": 0.3,    # 着差(レース内相対の標準得点)
    "deba_agari": 0.3,     # 上がり3F(レース内相対の標準得点)
    "deba_pace": 0.3,      # 脚質・先行力(コーナー通過順のレース内相対)
    "deba_interval": 0.2,  # レース間隔(休み明け/連闘)
    "deba_jockey": 0.2,    # 継続騎乗
    # 前走と今回のクラス差による近走点の補正強度(0で無効=従来通り、
    # 1で race_grade.class_adjust() の設計値どおり)。他のdeba系と同じ理由
    # (過学習リスク)で、まずは控えめな値から始める。
    "deba_class_adj": 0.5,
}

# 着順 → 点。前走ほど重い重みを掛けて平均する。
_RANK_PTS = {1: 8.0, 2: 5.0, 3: 3.0, 4: 1.0, 5: 1.0}
_RUN_WEIGHTS = (3.0, 2.5, 2.0, 1.5, 1.0)


def _rank_pts(chaku):
    if chaku is None:
        return None
    if chaku in _RANK_PTS:
        return _RANK_PTS[chaku]
    return -1.0 if chaku <= 9 else -2.5


def _features(h, cur_level=None, class_adj_strength=0.0):
    """1頭ぶんの特徴量。出馬表データが無ければ None
    cur_level: 今回のレースの格(race_grade.grade_level)。分かれば、前走
    以前との格差で近走点を補正する(重賞での善戦評価・昇格挑戦の割引等)。"""
    d = getattr(h, "deba", None)
    past = (d or {}).get("過去走") or []
    if not past:
        return None

    pts = wsum = 0.0
    class_note = None
    for run, w in zip(past, _RUN_WEIGHTS):
        chaku = run.get("着順")
        p = _rank_pts(chaku)
        if p is None:      # 中止・除外は評価から外す
            continue
        if cur_level is not None:
            past_level = race_grade.grade_level(run.get("レース名"))
            margin = None if chaku == 1 else run.get("着差")
            adj = race_grade.class_adjust(p, chaku, margin, past_level,
                                          cur_level, class_adj_strength)
            if run is past[0] and adj != p:
                class_note = (adj > p, past_level, cur_level)
            p = adj
        pts += p * w
        wsum += w
    recent = (pts / wsum / 4.0) if wsum else 0.0

    def avg(key, n=3, conv=lambda r, v: v):
        vals = [conv(r, r[key]) for r in past[:n] if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    # 着差は1着なら「後続を離した差」なので0(=差なし)として扱う
    margin = avg("着差", conv=lambda r, v: 0.0 if r.get("着順") == 1 else v)
    agari = avg("上がり3F")
    # コーナー通過順は最終コーナーの位置を頭数で正規化(小さいほど前)
    pace = None
    rel = [r["通過順"][-1] / r["頭数"] for r in past[:3]
           if r.get("通過順") and r.get("頭数")]
    if rel:
        pace = sum(rel) / len(rel)

    interval = None
    ymd = str(getattr(h, "race_date", "") or "")
    if past[0].get("日付") and re.fullmatch(r"20\d{6}", ymd):
        from datetime import date
        y, m, dd = (int(x) for x in past[0]["日付"].split("-"))
        interval = (date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
                    - date(y, m, dd)).days

    # 過去走の騎手名には減量記号(☆★▲△)が付くので外してから比べる
    last_jockey = re.sub(r"[☆★▲△]", "", past[0].get("騎手") or "").strip()

    return {
        "recent": recent, "margin": margin, "agari": agari, "pace": pace,
        "interval": interval,
        "jockey_cont": bool(last_jockey and h.jockey and last_jockey == h.jockey),
        "last_chaku": past[0].get("着順"),
        "good3": sum(1 for r in past[:3] if (r.get("着順") or 99) <= 3),
        "class_note": class_note,
    }


def _interval_pts(days):
    """レース間隔の評価。地方は中2〜3週が標準、休み明けは割引く"""
    if days is None:
        return 0.0
    if days <= 7:
        return -0.3          # 連闘は疲労リスク
    if days <= 28:
        return 1.0           # 順調なローテーション
    if days <= 60:
        return 0.3
    if days <= 120:
        return -0.5          # 休み明け
    return -1.0              # 長期休養明け


def _zmap(feats, key):
    """レース内で標準化した値のマップ。小さいほど良い指標なので符号を反転"""
    vals = [f[key] for f in feats.values() if f.get(key) is not None]
    if len(vals) < 3:
        return {}
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = var ** 0.5
    if sd < 1e-9:
        return {}
    return {u: -(f[key] - mean) / sd
            for u, f in feats.items() if f.get(key) is not None}


def adjust(horses, params, info=None):
    """出馬表由来の特徴量でスコアを補正する(レース単位で呼ぶ)。
    レース内で相対評価する指標があるため score_horse ではなくここで行う。
    info(今回のレース情報)があれば、前走以前とのクラス差で近走点を補正する。"""
    cur_level = None
    strength = params.get("deba_class_adj", 0.0)
    if info and strength > 0:
        cur_level = race_grade.grade_level(
            info.get("レース名"),
            "重賞" if info.get("競走種類") == "重賞" else "")
    feats = {h.umaban: f for h in horses
             if (f := _features(h, cur_level, strength))}
    if not feats:
        return
    z_margin = _zmap(feats, "margin")
    z_agari = _zmap(feats, "agari")
    z_pace = _zmap(feats, "pace")

    for h in horses:
        f = feats.get(h.umaban)
        if not f:
            continue
        u = h.umaban
        h.score += params.get("deba_recent", 0.0) * f["recent"]
        h.score += params.get("deba_margin", 0.0) * z_margin.get(u, 0.0)
        h.score += params.get("deba_agari", 0.0) * z_agari.get(u, 0.0)
        h.score += params.get("deba_pace", 0.0) * z_pace.get(u, 0.0)
        h.score += params.get("deba_interval", 0.0) * _interval_pts(f["interval"])
        if f["jockey_cont"]:
            h.score += params.get("deba_jockey", 0.0)

        h.reasons.extend(_reasons(h, f, z_margin.get(u), z_agari.get(u),
                                  z_pace.get(u), params))


def _reasons(h, f, zm, za, zp, params):
    out = []
    if params.get("deba_recent"):
        if f["last_chaku"] == 1:
            out.append("前走を勝っての臨戦")
        elif f["good3"] >= 2:
            out.append(f"近3走で{f['good3']}回の3着以内")
    note = f.get("class_note")
    if params.get("deba_class_adj") and note:
        improved, past_level, cur_level = note
        if improved:
            out.append("前走は格上相手の善戦(僅差 or 格上げ評価)")
        elif (past_level is not None and cur_level is not None
              and cur_level - past_level >= 2):
            # 1クラスだけの通常昇級はよくあるので割愛し、2段階以上の
            # 格上挑戦(重賞・オープン初挑戦など)のときだけ注記する
            out.append("前走は格下相手の好走、大幅な格上挑戦のため評価を割引")
    if params.get("deba_margin") and zm is not None and zm >= 0.8:
        out.append("近走の着差が小さく力量差はわずか")
    if params.get("deba_agari") and za is not None and za >= 0.8:
        out.append("上がり3Fが速く末脚は確か")
    if params.get("deba_pace") and zp is not None and zp >= 0.8:
        out.append("先行力があり流れに乗れる")
    if params.get("deba_interval"):
        if f["interval"] is not None and f["interval"] > 120:
            out.append(f"{f['interval']}日ぶりの長期休養明け")
        elif f["interval"] is not None and f["interval"] <= 7:
            out.append("連闘の疲労が心配")
    if params.get("deba_jockey") and f["jockey_cont"]:
        out.append(f"{h.jockey}騎手が継続騎乗")
    return out


# ============================================================
# CLI(キャッシュ作成)
# ============================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="出馬表ページから horselist CSV に無い馬情報を収集する")
    ap.add_argument("horselists", nargs="+", help="horselist CSV(複数可)")
    ap.add_argument("--refresh", action="store_true", help="キャッシュを無視して取り直す")
    args = ap.parse_args()

    import keiba_yosou as eng
    total = 0
    for f in args.horselists:
        path = Path(f)
        try:
            horses = eng.load_horselist(path)
        except Exception as e:
            print(f"⚠ {path.name}: 読み込み失敗 {e}", file=sys.stderr)
            continue
        races = {(h.place, h.race_no) for h in horses if h.place in BABA_CODES}
        if not races:
            print(f"⚠ {path.name}: 地方競馬のレースがありません(中央は非対応)")
            continue
        got, _ = fetch_day(path, races, refresh=args.refresh)
        total += got
    print(f"\n💾 合計{total}レース分を取得しました。予想時に自動で使われます。")


if __name__ == "__main__":
    main()
