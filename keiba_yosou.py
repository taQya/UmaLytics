#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
競馬 勝ち馬予想アプリ「予想チャットくん」 v3(中央・地方対応)
====================================================
出馬表CSV(horselist)を読み込み、競馬場×レースごとに勝ち馬を
予想チャット風に出力します。racelist / payback は同じフォルダに
あれば自動で読み込みます(手動指定も可)。

使い方:
    python keiba_yosou.py 20260704_horselist.csv
    python keiba_yosou.py 20260704_horselist.csv --place 船橋
    python keiba_yosou.py 20260704_horselist.csv --place 高知 --race 2
    python keiba_yosou.py 20260704_horselist.csv --json result.json

入力CSV(horselist)の主な列:
    競馬場, レース番号, 馬番, 馬名, 性, 齢, 騎手名, 騎手成績,
    負担重量, 馬体重, 馬体重増減, 全成績, 当競馬場成績,
    うち当距離成績, 人気
    ※成績は「1着-2着-3着-着外」形式(例: 9-22-14-85)

予想に使うのはレース前に分かる情報のみです。
horselist内の「着順」「タイム」列は予想には使用しません。
"""

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import deba_table   # 出馬表ページ(CSVに無い過去5走など)の収集とスコア補正
import horse_stats  # horses.sqlite3(馬の生涯レース履歴)由来のスコア補正


# ============================================================
# ユーティリティ
# ============================================================

# 中央競馬(JRA)の競馬場。ここに含まれれば中央モードで評価する
JRA_PLACES = {"札幌", "函館", "福島", "新潟", "東京", "中山",
              "中京", "京都", "阪神", "小倉"}


def is_jra(place: str) -> bool:
    return str(place).strip() in JRA_PLACES


def horse_is_jra(h) -> bool:
    """馬がJRA所属かを判定する。
    騎手所属または調教師所属の欄に「JRA」が含まれればJRA所属とみなす。
    (実データのトレセン表記 (栗東)/(美浦)/美浦・栗東 も補助的に判定)"""
    tokens = f"{getattr(h, 'jockey_affili', '')} {getattr(h, 'trainer_affili', '')}"
    if "JRA" in tokens:
        return True
    for kw in ("栗東", "美浦", "ＪＲＡ"):
        if kw in tokens:
            return True
    return False


def to_float(v, default=0.0):
    if v is None:
        return default
    s = str(v).strip().replace("＋", "+").replace("－", "-")
    if s in ("", "-", "―", "nan", "NaN"):
        return default
    s = re.sub(r"[^0-9.+\-]", "", s)  # ☆580 → 580
    if s in ("", "+", "-", "."):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def to_int(v, default=0):
    return int(to_float(v, default))


@dataclass
class Record:
    """「1-2-0-1」形式の成績(1着-2着-3着-着外)"""
    win: int = 0
    second: int = 0
    third: int = 0
    other: int = 0

    @property
    def starts(self):
        return self.win + self.second + self.third + self.other

    @property
    def win_rate(self):
        return self.win / self.starts if self.starts else 0.0

    @property
    def fukusho_rate(self):  # 複勝率(3着以内率)
        return (self.win + self.second + self.third) / self.starts if self.starts else 0.0


def parse_record(v) -> Record:
    if v is None:
        return Record()
    m = re.match(r"\s*(\d+)-(\d+)-(\d+)-(\d+)", str(v))
    if not m:
        return Record()
    return Record(*(int(x) for x in m.groups()))


@dataclass
class Horse:
    place: str
    race_no: int
    umaban: int
    name: str
    sex: str = ""
    age: int = 0
    jockey: str = ""
    jockey_rec: Record = field(default_factory=Record)
    kinryo: float = 0.0
    apprentice: bool = False        # ☆減量騎手
    weight: float = 0.0
    weight_diff: float = 0.0
    total_rec: Record = field(default_factory=Record)
    course_rec: Record = field(default_factory=Record)
    dist_rec: Record = field(default_factory=Record)
    turf_l: Record = field(default_factory=Record)
    turf_r: Record = field(default_factory=Record)
    dirt_l: Record = field(default_factory=Record)
    dirt_r: Record = field(default_factory=Record)
    ninki: int = 0
    odds: float = 0.0
    last3: list = field(default_factory=list)  # 直近3走の着順(新しい順)
    jockey_affili: str = ""        # 騎手所属(JRA/大井など)
    trainer_affili: str = ""       # 調教師所属
    race_date: str = ""            # 競走年月日 YYYYMMDD
    deba: dict = field(default_factory=dict)  # 出馬表ページ由来の情報(過去5走など)
    db: dict = field(default_factory=dict)    # horses.sqlite3由来の生涯レース履歴(初回のみ取得)
    score: float = 0.0
    reasons: list = field(default_factory=list)
    scratched: bool = False         # 出走取消/除外など(人気なし)


# ============================================================
# CSV読み込み
# ============================================================

def read_csv_dicts(path: Path):
    for enc in ("utf-8-sig", "cp932"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"文字コードを判別できません: {path}")


def g(row, *names, default=None):
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "nan"):
            return row[n]
    return default


def load_horselist(path: Path):
    rows = read_csv_dicts(path)
    horses = []
    for r in rows:
        place = str(g(r, "競馬場", default="")).strip()
        race_no = to_int(g(r, "レース番号"))
        umaban = to_int(g(r, "馬番"))
        if not place or race_no <= 0 or umaban <= 0:
            continue
        kinryo_raw = str(g(r, "負担重量", default="") or "")
        ninki = to_int(g(r, "人気"))
        horses.append(Horse(
            place=place,
            race_no=race_no,
            umaban=umaban,
            name=str(g(r, "馬名", default="")).strip(),
            sex=str(g(r, "性", default="") or "").strip(),
            age=to_int(g(r, "齢")),
            jockey=str(g(r, "騎手名", default="") or "").strip(),
            jockey_rec=parse_record(g(r, "騎手成績")),
            kinryo=to_float(kinryo_raw),
            apprentice=any(m in kinryo_raw for m in ("☆", "★", "▲", "△")),
            weight=to_float(g(r, "馬体重")),
            weight_diff=to_float(g(r, "馬体重増減")),
            total_rec=parse_record(g(r, "全成績")),
            course_rec=parse_record(g(r, "当競馬場成績")),
            dist_rec=parse_record(g(r, "うち当距離成績", "当距離成績")),
            turf_l=parse_record(g(r, "芝左成績")),
            turf_r=parse_record(g(r, "芝右成績")),
            dirt_l=parse_record(g(r, "ダート左成績")),
            dirt_r=parse_record(g(r, "ダート右成績")),
            ninki=ninki,
            odds=to_float(g(r, "オッズ", "単勝オッズ")),
            last3=[int(to_float(g(r, k))) or None
                   for k in ("前走着順", "2走前着順", "3走前着順")],
            jockey_affili=str(g(r, "騎手所属", default="") or "").strip(),
            trainer_affili=str(g(r, "調教師所属", "厩舎所属", default="") or "").strip(),
            race_date=str(g(r, "競走年月日", default="") or "").strip(),
            scratched=(ninki <= 0),
        ))
        st = str(g(r, "状態", default="") or "")
        if st:
            horses[-1].scratched = ("取消" in st or "除外" in st)
    for h in horses:
        if h.scratched and h.odds > 0:
            h.scratched = False
    if not horses:
        raise ValueError("horselistから有効な出走データを読み込めませんでした")
    deba_table.attach(horses, path)   # 出馬表ページ由来の情報(キャッシュがあれば)
    return horses


def load_racelist(path: Path):
    """(競馬場, レース番号) -> レース情報dict"""
    info = {}
    for r in read_csv_dicts(path):
        place = str(g(r, "競馬場", default="")).strip()
        race_no = to_int(g(r, "レース番号"))
        if not place or race_no <= 0:
            continue
        t = str(g(r, "発走時刻", default="") or "").strip()
        t = f"{t[:-2]}:{t[-2:]}" if t.isdigit() and len(t) >= 3 else t
        info[(place, race_no)] = {
            "レース名": str(g(r, "レース名", default="") or "").strip(),
            "発走時刻": t,
            "距離": to_int(g(r, "距離")),
            "芝ダート": str(g(r, "芝ダート区分", default="") or "").strip(),
            "回り": str(g(r, "回り", default="") or "").strip(),
            "頭数": to_int(g(r, "頭数")),
            "天候": str(g(r, "天候", default="") or "").strip(),
        }
    return info


def load_payback(path: Path):
    """(競馬場, レース番号) -> {'勝ち馬番': int, '単勝払戻': int}"""
    result = {}
    for r in read_csv_dicts(path):
        place = str(g(r, "競馬場", default="")).strip()
        race_no = to_int(g(r, "レース番号"))
        win_uma = to_int(g(r, "単勝組番"))
        pay = to_int(g(r, "単勝払戻金（円）", "単勝払戻金(円)"))
        fuku = [to_int(g(r, f"複勝組番{i}")) for i in (1, 2, 3)]
        fuku = [u for u in fuku if u > 0]
        if place and race_no > 0 and win_uma > 0:
            # 着順マップ: 1着=単勝組番。2着は馬単組番2、3着は三連単組番3
            # (JRA結果形式の2着馬番/3着馬番列にも対応)。特定できない馬のみ
            # 複勝組番から「3着内」として扱う。
            chaku = {win_uma: "1着"}
            u2 = to_int(g(r, "2着馬番", "馬単組番2"))
            u3 = to_int(g(r, "3着馬番", "３連単組番馬番3", "3連単組番馬番3"))
            if u2 > 0:
                chaku[u2] = "2着"
            if u3 > 0 and u3 not in chaku:
                chaku[u3] = "3着"
            # 三連単が無い場合: 複勝組番のうち1・2着以外の残り1頭を3着と確定
            rest = [u for u in fuku if u not in chaku]
            if len(rest) == 1 and "3着" not in chaku.values():
                chaku[rest[0]] = "3着"
            for u in fuku:
                chaku.setdefault(u, "3着内")

            # --- 組合せ馬券の実払戻(的中判定用) ---
            combos = {}
            uq1, uq2 = to_int(g(r, "馬複組番1")), to_int(g(r, "馬複組番2"))
            if uq1 and uq2:
                combos["馬複"] = (frozenset({uq1, uq2}),
                                  to_int(g(r, "馬複払戻金（円）", "馬複払戻金(円)")))
            ex1, ex2 = to_int(g(r, "馬単組番1")), to_int(g(r, "馬単組番2"))
            if ex1 and ex2:
                combos["馬単"] = ((ex1, ex2),
                                  to_int(g(r, "馬単払戻金（円）", "馬単払戻金(円)")))
            wides = []
            for i in (1, 2, 3):
                w1 = to_int(g(r, f"ワイド組番{i}馬番1"))
                w2 = to_int(g(r, f"ワイド組番{i}馬番2"))
                wp = to_int(g(r, f"ワイド払戻金{i}（円）", f"ワイド払戻金{i}(円)"))
                if w1 and w2:
                    wides.append((frozenset({w1, w2}), wp))
            if wides:
                combos["ワイド"] = wides
            t1 = to_int(g(r, "３連複組番馬番1", "3連複組番馬番1"))
            t2 = to_int(g(r, "３連複組番馬番2", "3連複組番馬番2"))
            t3 = to_int(g(r, "３連複組番馬番3", "3連複組番馬番3"))
            if t1 and t2 and t3:
                combos["三連複"] = (frozenset({t1, t2, t3}),
                                    to_int(g(r, "３連複払戻金（円）", "3連複払戻金(円)")))

            result[(place, race_no)] = {
                "勝ち馬番": win_uma, "単勝払戻": pay, "複勝馬番": fuku,
                "着順マップ": chaku, "組合せ払戻": combos,
            }
    return result


def find_sibling(horselist_path: Path, keyword: str):
    """horselist CSV と同じディレクトリにある racelist / payback を探す。
    ファイル名の 'horselist' 部分だけを keyword に置換して完全一致で探すため、
    以下の命名規則にそのまま対応する(同ディレクトリ内・完全一致):
      - 地方競馬: YYYYMMDD_horselist.csv → YYYYMMDD_payback.csv
      - 中央競馬: JRA_YYYY_MMDD_horselist.csv → JRA_YYYY_MMDD_payback.csv
    """
    hp = Path(horselist_path)
    # ファイル名部分だけを置換する(親ディレクトリ名に 'horselist' が
    # 含まれていても影響を受けないようにする)。
    if "horselist" in hp.name:
        cand = hp.with_name(hp.name.replace("horselist", keyword))
        if cand != hp and cand.exists():
            return cand
    # フォールバック: 同じディレクトリ内で keyword を含むCSVを探す。
    # 日付プレフィックスが一致するものを優先する。
    import re
    m = re.search(r"(20\d{2}[_-]?\d{2}[_-]?\d{2})", hp.name)
    ymd = re.sub(r"[_-]", "", m.group(1)) if m else None
    cands = sorted(hp.parent.glob(f"*{keyword}*.csv"))
    if ymd:
        for c in cands:
            if ymd in re.sub(r"[_-]", "", c.name):
                return c
    if len(cands) == 1:
        return cands[0]
    return None

def detect_ymd_from_name(path, fallback=None):
    """ファイル名から開催日 YYYYMMDD を検出する。
    対応する命名規則:
      - 地方競馬: YYYYMMDD_horselist.csv        (例: 20260704_horselist.csv)
      - 中央競馬: JRA_YYYY_MMDD_*.csv           (例: JRA_2026_0705_horselist.csv)
      - 旧中央:   jra_YYYYMMDD_horselist.csv    (後方互換)
    見つからなければ fallback を返す。"""
    import re
    name = Path(path).name
    # JRA_YYYY_MMDD 形式(アンダースコア区切り)
    m = re.search(r"(20\d{2})[_-](\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    # 連続8桁 YYYYMMDD
    m = re.search(r"(20\d{6})", name)
    if m:
        return m.group(1)
    return fallback



# ============================================================
# 予想スナップショット(確定)
# ============================================================
# 人気・オッズは締切まで変動するため、データを取得し直すたびに予想が
# 変わってしまう問題への対策。最初に予想した時点のスコアをJSONに保存し、
# 同じ開催日の再実行ではそのスコアを使って印・順位を固定する。
# ※期待値買いは「確定した勝率 × 最新オッズ」で毎回再計算される(意図的)。

def snapshot_file(horselist_path):
    """horselistと同じディレクトリの確定ファイルパスを返す"""
    hp = Path(horselist_path)
    ymd = detect_ymd_from_name(hp, None) or "today"
    return hp.parent / f"yosou_fixed_{ymd}.json"


def load_snapshot(horselist_path):
    f = snapshot_file(horselist_path)
    if not f.exists():
        return None
    try:
        import json as _json
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_snapshot(horselist_path, races_scores):
    """races_scores: {"場|R": {"scores": {"馬番": score}}} を保存"""
    import json as _json
    from datetime import datetime as _dt
    f = snapshot_file(horselist_path)
    data = {"確定時刻": _dt.now().strftime("%Y-%m-%d %H:%M"),
            "races": races_scores}
    f.write_text(_json.dumps(data, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return f


def apply_snapshot(ranked, snap_scores):
    """確定済みスコアを適用して並べ直す。
    確定後に出走取消となった馬は自然に除外され、確定時に居なかった馬
    (出走変更など)は計算済みスコアのまま末尾側に並ぶ。"""
    for h in ranked:
        s = snap_scores.get(str(h.umaban))
        if s is not None:
            h.score = float(s)
    return sorted(ranked, key=lambda x: x.score, reverse=True)


# ============================================================
# スコアリング(レース前情報のみ使用)
# ============================================================

# ============================================================
# チューニング可能パラメータ
# ============================================================
# keiba_params.json(同フォルダ)があれば起動時に上書きされます。
# tune_params.py で結果データから自動調整できます。
PARAMS = {
    "total_win": 40.0,   # 通算勝率の係数
    "total_fuku": 20.0,  # 通算複勝率の係数
    "recent": 0.6,       # 近走成績のスケール
    "apt": 1.0,          # 適性(当場/当距離/馬場)のスケール
    "jockey": 1.0,       # 騎手評価のスケール
    "ninki": 1.0,        # 人気点のスケール(0で人気を参考にしない)
    "kinryo": 200.0,     # 斤量(平均比)の係数
    "softmax_t": 10.0,   # スコア→勝率の温度
    "jra_affili": 8.0,   # JRA所属馬への加点(交流重賞での実力差を考慮)
}
# 出馬表ページ由来の重み(deba_recent/margin/agari/pace/interval/jockey)
PARAMS.update(deba_table.PARAMS_DEFAULT)
# horses.sqlite3(生涯レース履歴)由来の重み(db_recent/dist/jockey/interval)
PARAMS.update(horse_stats.PARAMS_DEFAULT)


def app_dir():
    """データファイル(keiba_params.json等)の基準ディレクトリを返す。
    PyInstallerでexe化された場合は実行ファイルのある場所、
    通常実行ではこのスクリプトのある場所を返す。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):        # exe実行時
        return Path(_sys.executable).parent
    return Path(__file__).parent


def load_params():
    import json as _json
    for base in (app_dir(), Path.cwd()):
        f = base / "keiba_params.json"
        if f.exists():
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                PARAMS.update({k: float(v) for k, v in data.items()
                               if k in PARAMS})
                return f
            except Exception:
                pass
    return None


_PARAMS_FILE = load_params()


def merge_rec(a: Record, b: Record) -> Record:
    return Record(a.win + b.win, a.second + b.second,
                  a.third + b.third, a.other + b.other)


def surface_record(h: Horse, info) -> Record:
    """レースの馬場(芝/ダート)と回り(右/左)に合う成績を返す"""
    if not info:
        return Record()
    surf = info.get("芝ダート", "")
    mawari = info.get("回り", "")
    if "芝" in surf:
        pair = (h.turf_l, h.turf_r)
    elif "ダ" in surf:
        pair = (h.dirt_l, h.dirt_r)
    else:
        return Record()
    if "左" in mawari:
        return pair[0]
    if "右" in mawari:
        return pair[1]
    return merge_rec(*pair)


def score_horse(h: Horse, info=None):
    score = 50.0
    reasons = []

    # --- 馬の実力: 全成績 ---
    tr = h.total_rec
    if tr.starts > 0:
        conf = min(1.0, tr.starts / 10)  # 出走数が少ないと信頼度を割引
        score += (tr.win_rate * PARAMS["total_win"]
                  + tr.fukusho_rate * PARAMS["total_fuku"]) * conf
        if tr.win_rate >= 0.25 and tr.starts >= 5:
            reasons.append(f"通算勝率{tr.win_rate:.0%}の実力馬")
        elif tr.fukusho_rate >= 0.5 and tr.starts >= 5:
            reasons.append(f"複勝率{tr.fukusho_rate:.0%}と堅実")
    else:
        score -= 3
        reasons.append("キャリア浅く未知数")

    # --- 当競馬場・当距離適性 ---
    # --- JRA所属の実力差(交流重賞などで地方所属より上位が多い) ---
    if horse_is_jra(h):
        score += PARAMS["jra_affili"]
        if PARAMS["jra_affili"] >= 4:
            reasons.append("JRA所属の実績馬")

    # --- 近走成績(前走>2走前>3走前) ---
    if any(c for c in h.last3):
        pts = 0.0
        for chaku, w in zip(h.last3, (3.0, 2.0, 1.0)):
            if not chaku:
                continue
            if chaku == 1:
                pts += 8 * w
            elif chaku == 2:
                pts += 5 * w
            elif chaku == 3:
                pts += 3 * w
            elif chaku <= 5:
                pts += 1 * w
            elif chaku <= 9:
                pts -= 1 * w
            else:
                pts -= 2.5 * w
        score += pts * PARAMS["recent"]
        good = [c for c in h.last3 if c and c <= 3]
        if h.last3 and h.last3[0] == 1:
            reasons.append("前走勝ちの勢いあり")
        elif len(good) >= 2:
            reasons.append(f"近走{len(good)}回の3着以内で好調")
        elif h.last3 and h.last3[0] and h.last3[0] >= 10:
            reasons.append(f"前走{h.last3[0]}着からの巻き返し待ち")

    cr, dr = h.course_rec, h.dist_rec
    if cr.starts >= 3:
        score += (cr.win_rate * 15 + cr.fukusho_rate * 8) * PARAMS["apt"]
        if cr.win_rate >= 0.25:
            reasons.append("当競馬場を得意にしている")
    if dr.starts >= 3:
        score += (dr.win_rate * 15 + dr.fukusho_rate * 8) * PARAMS["apt"]
        if dr.win_rate >= 0.25:
            reasons.append("この距離での勝率が高い")

    # --- 騎手 ---
    # --- 馬場適性(芝/ダート×回り: 中央競馬データで特に有効) ---
    sr = surface_record(h, info)
    if sr.starts >= 3:
        score += (sr.win_rate * 15 + sr.fukusho_rate * 8) * PARAMS["apt"]
        if sr.win_rate >= 0.25:
            surf = (info or {}).get("芝ダート", "この馬場")
            reasons.append(f"{surf}コースを得意にしている")

    jr = h.jockey_rec
    if jr.starts >= 5:
        if jr.win_rate >= 0.25:
            score += 8 * PARAMS["jockey"]
            reasons.append(f"{h.jockey}騎手が好調(勝率{jr.win_rate:.0%})")
        elif jr.win_rate >= 0.15:
            score += 4 * PARAMS["jockey"]
        elif jr.win_rate < 0.05 and jr.starts >= 10:
            score -= 3
    if h.apprentice:
        score += 2
        reasons.append("減量騎手の恩恵あり")

    # --- 人気(市場評価) ---
    # PARAMS["ninki"] が 0 のときは人気を一切参考にしない(加減点も根拠タグも無し)
    npt = PARAMS["ninki"]
    if npt != 0:
        if h.ninki == 1:
            score += 12 * npt; reasons.append("1番人気の支持")
        elif h.ninki == 2:
            score += 8 * npt
        elif h.ninki == 3:
            score += 5 * npt
        elif 4 <= h.ninki <= 6:
            score += 1 * npt
        elif 7 <= h.ninki <= 9:
            score -= 4 * npt
        elif h.ninki >= 10:
            score -= 8 * npt

    # --- 馬体重の増減(体重比で判定: ばんえい馬対応) ---
    if h.weight > 0 and h.weight_diff != 0:
        ratio = abs(h.weight_diff) / h.weight
        if ratio >= 0.02:      # 平地500kg馬で±10kg以上に相当
            score -= 5
            reasons.append(f"馬体重{h.weight_diff:+.0f}kgの大幅変動は不安")
        elif ratio >= 0.012:
            score -= 2

    # --- 高齢 ---
    if h.age >= 10:
        score -= 3
    elif h.age >= 8:
        score -= 1

    h.score = score
    h.reasons = reasons


def adjust_kinryo(horses):
    """レース内平均との負担重量差で微調整(単位系が違っても相対比較でOK)"""
    ks = [h.kinryo for h in horses if h.kinryo > 0]
    if len(ks) < 2:
        return
    avg = sum(ks) / len(ks)
    if avg <= 0:
        return
    for h in horses:
        if h.kinryo > 0:
            # 平均比1%重いごとに-2点
            adj = -((h.kinryo - avg) / avg) * PARAMS["kinryo"]
            h.score += adj
            if adj >= 3:
                h.reasons.append("ハンデ差(軽量)は大きな武器")
            elif adj <= -4:
                h.reasons.append("重い負担重量がカギ")




def predict_race(horses, info=None):
    """1レース分を予想して並べ替えたリストを返す"""
    for h in horses:
        score_horse(h, info)
    adjust_kinryo(horses)
    deba_table.adjust(horses, PARAMS)   # 近走・脚質はレース内相対で効かせる
    horse_stats.adjust(horses, info, PARAMS)  # 生涯レース履歴由来の補正
    return sorted(horses, key=lambda x: x.score, reverse=True)


# ============================================================
# 期待値買い(単勝)
# ============================================================
# モデルスコア→softmaxで勝率推定、人気→Zipf近似で推定オッズを算出し、
# 期待値(勝率×推定オッズ)が閾値以上の馬だけ買い、なければ見送り。
# ※推定オッズは概算。期待値100%超は見込みであり利益の保証ではありません。

SOFTMAX_T = 10.0
TAKEOUT_RETURN = 0.75      # 地方 単勝払戻率
TAKEOUT_RETURN_JRA = 0.80  # 中央 単勝払戻率
EV_MAX_BETS = 2
ZIPF_ALPHA = 1.35


def model_probs(ranked):
    mx = max(h.score for h in ranked)
    ws = [math.exp((h.score - mx) / PARAMS["softmax_t"]) for h in ranked]
    s = sum(ws)
    return [w / s for w in ws]


def load_odds_calibration():
    """calibrate_odds.py が実際の払戻データから作った市場シェア曲線を読み込む
    (地方競馬のみ対象。中央競馬は的中データが無いため従来のZipf近似のまま)"""
    import json as _json
    for base in (app_dir(), Path.cwd()):
        f = base / "odds_calibration.json"
        if f.exists():
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                return {int(k): v for k, v in data["shares"].items()}
            except Exception:
                pass
    return None


ODDS_SHARES = load_odds_calibration()


def estimate_odds_map(ranked, payout=TAKEOUT_RETURN, jra=False):
    """実オッズ列があれば優先、無い馬は人気から推定 {馬番: (オッズ, 実か)}
    地方競馬は ODDS_SHARES(実データ校正済み)があればそれを使い、
    無ければ/中央競馬は従来のZipf近似(rank^-ZIPF_ALPHA)にフォールバックする"""
    n = len(ranked)
    raw = {}
    for h in ranked:
        rank = h.ninki if h.ninki > 0 else n
        if not jra and ODDS_SHARES and rank in ODDS_SHARES:
            raw[h.umaban] = ODDS_SHARES[rank]
        else:
            raw[h.umaban] = rank ** (-ZIPF_ALPHA)
    s = sum(raw.values())
    out = {}
    for h in ranked:
        if h.odds > 1.0:
            out[h.umaban] = (h.odds, True)
        else:
            out[h.umaban] = (max(1.1, payout / (raw[h.umaban] / s)), False)
    return out


def ev_bets(ranked, threshold=100.0, payout=TAKEOUT_RETURN, jra=False):
    probs = model_probs(ranked)
    odds_map = estimate_odds_map(ranked, payout, jra)
    picks = []
    for h, p in zip(ranked, probs):
        odds, real = odds_map[h.umaban]
        ev = p * odds * 100
        if ev >= threshold:
            picks.append({"horse": h, "prob": p, "odds": odds,
                          "ev": ev, "real": real})
    picks.sort(key=lambda x: -x["ev"])
    return picks[:EV_MAX_BETS]

# ============================================================
# 印からの組合せ馬券 期待値計算(馬単・馬複・ワイド・三連複)
# ============================================================
# モデル勝率(model_probs)と市場勝率(オッズ/人気由来)からHarville法で
# 各組合せの的中確率を求め、推定配当(払戻率/市場確率)と掛けて期待値を出す。
# 単勝の期待値買いと同じ「モデルと市場の見解が割れた組合せ」を拾う考え方。

COMBO_PAYOUT = {  # 券種別の払戻率(概算)
    True:  {"馬複": 0.775, "馬単": 0.75, "ワイド": 0.775, "三連複": 0.75},   # 中央
    False: {"馬複": 0.75,  "馬単": 0.75, "ワイド": 0.75,  "三連複": 0.725},  # 地方
}


def _market_prob_map(ranked, payout, jra=False):
    """市場の勝率マップ {馬番: p}。実オッズがあれば1/オッズ比例、無ければ人気から推定"""
    om = estimate_odds_map(ranked, payout, jra)
    raw = {u: 1.0 / o for u, (o, _real) in om.items()}
    s = sum(raw.values())
    return {u: v / s for u, v in raw.items()}


def _harville_pair(p, a, b):
    """a→b(馬単)の確率"""
    return p[a] * p[b] / max(1e-9, 1 - p[a])


def _harville_top3(p_map):
    """全馬の順序付き3着以内を列挙し、
    (三連複setの確率dict, ワイドpairの確率dict) を返す"""
    from itertools import permutations
    umas = list(p_map)
    trio = {}
    wide = {}
    for a, b, c in permutations(umas, 3):
        pa, pb, pc = p_map[a], p_map[b], p_map[c]
        pr = pa * pb / max(1e-9, 1 - pa) * pc / max(1e-9, 1 - pa - pb)
        key3 = frozenset({a, b, c})
        trio[key3] = trio.get(key3, 0.0) + pr
    for key3, pr in trio.items():
        for pair in ({x, y} for x in key3 for y in key3 if x < y):
            fk = frozenset(pair)
            wide[fk] = wide.get(fk, 0.0) + pr
    return trio, wide


def ev_combo_bets(ranked, threshold=100.0, jra=False, top_n=4, per_type=2):
    """印上位top_n頭の組合せから、期待値がしきい値以上の買い目を返す。
    戻り値: [{"券種","組","確率","推定配当","ev"}...](期待値降順)"""
    if len(ranked) < 3:
        return []
    payout_win = TAKEOUT_RETURN_JRA if jra else TAKEOUT_RETURN
    pm = dict(zip((h.umaban for h in ranked), model_probs(ranked)))
    pk = _market_prob_map(ranked, payout_win, jra)
    rates = COMBO_PAYOUT[bool(jra)]
    tops = [h.umaban for h in ranked[:top_n]]

    m_trio, m_wide = _harville_top3(pm)
    k_trio, k_wide = _harville_top3(pk)

    from itertools import combinations, permutations
    cands = []

    def add(kind, key, disp, p_model, p_mkt):
        if p_model <= 0 or p_mkt <= 0:
            return
        est = rates[kind] / p_mkt
        ev = p_model * est * 100
        if ev >= threshold:
            cands.append({"券種": kind, "組": key, "表示": disp,
                          "確率": p_model, "推定配当": est, "ev": ev})

    for a, b in combinations(tops, 2):
        add("馬複", frozenset({a, b}), f"{a}-{b}",
            _harville_pair(pm, a, b) + _harville_pair(pm, b, a),
            _harville_pair(pk, a, b) + _harville_pair(pk, b, a))
        add("ワイド", frozenset({a, b}), f"{a}-{b}",
            m_wide.get(frozenset({a, b}), 0), k_wide.get(frozenset({a, b}), 0))
    for a, b in permutations(tops, 2):
        add("馬単", (a, b), f"{a}→{b}",
            _harville_pair(pm, a, b), _harville_pair(pk, a, b))
    for combo in combinations(tops, 3):
        key = frozenset(combo)
        disp = "-".join(str(x) for x in sorted(combo))
        add("三連複", key, disp, m_trio.get(key, 0), k_trio.get(key, 0))

    # 券種ごとに上位per_typeに絞り、全体を期待値降順で返す
    cands.sort(key=lambda x: -x["ev"])
    out, count = [], {}
    for c in cands:
        k = c["券種"]
        if count.get(k, 0) >= per_type:
            continue
        count[k] = count.get(k, 0) + 1
        out.append(c)
    return out


def combo_hit(pick, payback):
    """組合せ買い目の的中判定。的中なら実払戻金、外れなら0を返す(判定不能はNone)"""
    combos = (payback or {}).get("組合せ払戻", {})
    kind, key = pick["券種"], pick["組"]
    if kind == "ワイド":
        for fs, pay in combos.get("ワイド", []):
            if fs == key:
                return pay
        return 0 if combos.get("ワイド") else None
    entry = combos.get(kind)
    if entry is None:
        return None
    return entry[1] if entry[0] == key else 0


# ============================================================
# 出力
# ============================================================

MARKS = ["◎", "○", "▲", "△", "☆"]


def confidence_label(ranked):
    if len(ranked) < 2:
        return "参考", "-"
    gap = ranked[0].score - ranked[1].score
    if gap >= 10:
        return "鉄板級", "S"
    if gap >= 6:
        return "自信あり", "A"
    if gap >= 3:
        return "有力", "B"
    return "混戦・波乱注意", "C"


def chat_print(place, race_no, ranked, race_info, payback, top_n, picks, combo_picks=None):
    top = ranked[0]
    conf_text, conf_rank = confidence_label(ranked)

    mode = "中央" if is_jra(place) else "地方"
    header = f"🏇 【{mode}】{place} 第{race_no}レース"
    if race_info and race_info.get("レース名"):
        header += f"「{race_info['レース名']}」"
    print(f"\n{'─' * 56}")
    print(header)
    if race_info:
        meta = []
        if race_info.get("発走時刻"):
            meta.append(f"発走{race_info['発走時刻']}")
        if race_info.get("芝ダート") and race_info.get("距離"):
            mawari = race_info.get("回り", "")
            m_s = f"({mawari}回り)" if mawari and mawari != "直" else ""
            meta.append(f"{race_info['芝ダート']}{m_s}{race_info['距離']}m")
        if race_info.get("頭数"):
            meta.append(f"{race_info['頭数']}頭")
        if race_info.get("天候"):
            meta.append(f"天候:{race_info['天候']}")
        if meta:
            print("   " + " / ".join(meta))
    print(f"{'─' * 56}")

    print(f"💬 本命は ◎ {top.umaban}番【{top.name}】(信頼度{conf_rank}:{conf_text})")
    if top.ninki > 0:
        print(f"   {top.ninki}番人気 / {top.jockey}騎手")
    for r in top.reasons[:3]:
        print(f"   ・{r}")
    if not top.reasons:
        print("   ・総合力で一枚上と見た!")

    print("📋 印まとめ:")
    for i, h in enumerate(ranked[:top_n]):
        mark = MARKS[i] if i < len(MARKS) else " "
        ninki_s = f"{h.ninki}人気" if h.ninki > 0 else "  -  "
        print(f"   {mark} {h.umaban:>2}番 {h.name:<10} スコア{h.score:6.1f} ({ninki_s}/{h.jockey})")

    if len(ranked) >= 3:
        a, b, c = ranked[0].umaban, ranked[1].umaban, ranked[2].umaban
        print(f"🎯 参考買い目: 単勝 {a} / 馬複 {a}-{b} / 三連複 {a}-{b}-{c}")
    if picks:
        for pk in picks:
            h = pk["horse"]
            o_label = "実" if pk.get("real") else "推定"
            print(f"💰 期待値買い: 単勝 {h.umaban}番 {h.name} "
                  f"(推定勝率{pk['prob']:.0%}×{o_label}{pk['odds']:.1f}倍=期待値{pk['ev']:.0f}%)")
    else:
        print("💰 期待値買い: 見送り(期待値が閾値未満)")
    for c in (combo_picks or []):
        print(f"🎲 組合せ期待値買い: [{c['券種']}] {c['表示']} "
              f"(的中確率{c['確率']:.1%}×推定{c['推定配当']:.1f}倍=期待値{c['ev']:.0f}%)")
    if conf_rank == "C":
        print("💬 このレースは混戦模様。手広く構えるのが吉かも!")

    # --- 答え合わせ ---
    hit = fuku_hit = None
    if payback:
        win = payback["勝ち馬番"]
        pay = payback["単勝払戻"]
        hit = (top.umaban == win)
        fuku_hit = (top.umaban in payback.get("複勝馬番", []))
        win_horse = next((h for h in ranked if h.umaban == win), None)
        win_name = f"【{win_horse.name}】" if win_horse else ""
        if hit:
            print(f"✅ 結果: {win}番{win_name}が勝利! ◎的中🎉 (単勝{pay}円)")
        elif fuku_hit:
            our_rank = next((i + 1 for i, h in enumerate(ranked) if h.umaban == win), None)
            rank_s = f"(こちらは予想{our_rank}位評価)" if our_rank else ""
            print(f"🔶 結果: 勝ったのは{win}番{win_name}{rank_s}。◎は3着以内で複勝圏キープ!")
        else:
            our_rank = next((i + 1 for i, h in enumerate(ranked) if h.umaban == win), None)
            rank_s = f"(予想{our_rank}位評価)" if our_rank else ""
            print(f"❌ 結果: 勝ったのは{win}番{win_name}{rank_s}。◎は外れ…次いくよ!")
        # --- 印と結果の対照 ---
        cm = payback.get("着順マップ", {})
        pairs = []
        for i, h in enumerate(ranked[:5]):
            mk = MARKS[i] if i < len(MARKS) else ""
            pairs.append(f"{mk}→{cm.get(h.umaban, '圏外')}")
        in_ken = sum(1 for h in ranked[:3] if h.umaban in cm)
        print(f"🏅 印別結果: {' ／ '.join(pairs)} (印上位3頭中{in_ken}頭が3着以内)")
        # --- 期待値買いの結果・収支 ---
        if picks:
            lines, t_in, t_out = [], 0, 0
            for pk in picks:
                u = pk["horse"].umaban
                t_out += 100
                if u == win:
                    t_in += payback["単勝払戻"]
                    lines.append(f"単勝{u}番:的中🎯+{payback['単勝払戻'] - 100}円")
                elif u in cm:
                    lines.append(f"単勝{u}番:{cm[u]}(単勝外れ)")
                else:
                    lines.append(f"単勝{u}番:外れ")
            net = t_in - t_out
            print(f"💰 期待値買い結果: {' ／ '.join(lines)} → 収支{'+' if net >= 0 else ''}{net}円")
        if combo_picks:
            clines, c_in, c_out = [], 0, 0
            for c in combo_picks:
                hp = combo_hit(c, payback)
                if hp is None:
                    clines.append(f"[{c['券種']}]{c['表示']}:判定不能")
                    continue
                c_out += 100
                if hp > 0:
                    c_in += hp
                    clines.append(f"[{c['券種']}]{c['表示']}:的中🎯+{hp - 100}円")
                else:
                    clines.append(f"[{c['券種']}]{c['表示']}:外れ")
            if c_out:
                cnet = c_in - c_out
                print(f"🎲 組合せ買い結果: {' ／ '.join(clines)} → 収支{'+' if cnet >= 0 else ''}{cnet}円")
    return hit, fuku_hit


def run(horselist: Path, racelist, payback_path, place_filter, race_filter,
        top_n, json_out, ev_threshold=100.0):
    horses = load_horselist(horselist)
    race_info = load_racelist(racelist) if racelist else {}
    paybacks = load_payback(payback_path) if payback_path else {}

    races = {}
    for h in horses:
        if h.scratched:
            continue
        races.setdefault((h.place, h.race_no), []).append(h)

    keys = sorted(races.keys(), key=lambda k: (k[0], k[1]))
    if place_filter:
        keys = [k for k in keys if place_filter in k[0]]
    if race_filter:
        keys = [k for k in keys if k[1] == race_filter]
    if not keys:
        raise ValueError("条件に合うレースがありません(--place / --race を確認してください)")

    places = sorted({k[0] for k in keys})
    print("╔══════════════════════════════════════════════════╗")
    print("║   🐴 競馬 予想チャットくん v3(中央・地方対応) 🐴  ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"📂 {horselist.name}: {sum(len(races[k]) for k in keys)}頭 / {len(keys)}レース")
    print(f"🏟️ 対象: {'、'.join(places)}")
    if racelist:
        print(f"📄 レース情報: {Path(racelist).name}")
    if payback_path:
        print(f"💰 払戻データ: {Path(payback_path).name}(終了レースは答え合わせします)")

    result = {}
    hits = fuku_hits = graded = invest = ret = 0
    ev_hits = ev_invest = ev_ret = ev_skip = 0
    for key in keys:
        rh = races[key]
        info = race_info.get(key)
        jra = is_jra(key[0])
        ranked = predict_race(rh, info)
        pb = paybacks.get(key)
        payout = TAKEOUT_RETURN_JRA if jra else TAKEOUT_RETURN
        picks = ev_bets(ranked, ev_threshold, payout, jra)
        combo_picks = ev_combo_bets(ranked, ev_threshold, jra)
        hit, fuku_hit = chat_print(key[0], key[1], ranked, race_info.get(key), pb,
                                   top_n, picks, combo_picks)
        if pb:
            if not picks:
                ev_skip += 1
            for pk in picks:
                ev_invest += 100
                if pk["horse"].umaban == pb["勝ち馬番"]:
                    ev_hits += 1
                    ev_ret += pb["単勝払戻"]
        if hit is not None:
            graded += 1
            invest += 100
            if hit:
                hits += 1
                ret += pb["単勝払戻"]
            if fuku_hit or hit:
                fuku_hits += 1
        result[f"{key[0]}_{key[1]}R"] = {
            "レース名": (race_info.get(key) or {}).get("レース名", ""),
            "期待値買い_単勝": [
                {"馬番": pk["horse"].umaban, "馬名": pk["horse"].name,
                 "推定勝率": round(pk["prob"], 3), "推定オッズ": round(pk["odds"], 1),
                 "期待値pct": round(pk["ev"], 1)}
                for pk in picks
            ] or "見送り",
            "予想": [
                {"順位": i + 1, "印": MARKS[i] if i < len(MARKS) else "",
                 "馬番": h.umaban, "馬名": h.name, "騎手": h.jockey,
                 "人気": h.ninki, "スコア": round(h.score, 1), "根拠": h.reasons}
                for i, h in enumerate(ranked)
            ],
            "結果": ({"勝ち馬番": pb["勝ち馬番"], "単勝払戻": pb["単勝払戻"],
                      "本命的中": hit} if pb else None),
        }

    print(f"\n{'═' * 56}")
    if graded:
        rate = hits / graded * 100
        recov = ret / invest * 100 if invest else 0
        frate = fuku_hits / graded * 100
        print(f"📊 答え合わせ(終了{graded}レース):")
        print(f"   ◎単勝的中: {hits}/{graded} ({rate:.0f}%) / ◎3着以内: {fuku_hits}/{graded} ({frate:.0f}%)")
        print(f"   単勝100円ずつ購入した場合: 投資{invest}円 → 払戻{ret}円 (回収率{recov:.0f}%)")
        if ev_invest:
            ev_recov = ev_ret / ev_invest * 100
            print(f"   💰期待値買い(閾値{ev_threshold:.0f}%): {ev_invest // 100}点購入(見送り{ev_skip}R)・的中{ev_hits}"
                  f" / 投資{ev_invest}円 → 払戻{ev_ret}円 (回収率{ev_recov:.0f}%)")
    print("💬 予想チャットくん: 今日の予想は以上!")
    print("   あくまで参考予想。馬券は余裕資金の範囲で楽しんでね🐴")

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"📝 予想結果をJSONに保存: {json_out}")


def main():
    p = argparse.ArgumentParser(description="競馬 勝ち馬予想アプリ v3(中央・地方対応)")
    p.add_argument("horselist", type=Path, help="出馬表CSV(*_horselist.csv)")
    p.add_argument("--racelist", type=Path, default=None,
                   help="レース情報CSV(省略時は同フォルダから自動検出)")
    p.add_argument("--payback", type=Path, default=None,
                   help="払戻CSV(省略時は同フォルダから自動検出)")
    p.add_argument("--place", type=str, default=None, help="競馬場で絞り込み(例: 船橋)")
    p.add_argument("--race", type=int, default=None, help="レース番号で絞り込み")
    p.add_argument("--top", type=int, default=5, help="印を付ける頭数(デフォルト5)")
    p.add_argument("--ev", type=float, default=100.0,
                   help="期待値買いの閾値%%(デフォルト100)")
    p.add_argument("--json", type=Path, default=None, help="結果をJSON保存")
    args = p.parse_args()

    if not args.horselist.exists():
        print(f"❌ ファイルが見つかりません: {args.horselist}", file=sys.stderr)
        sys.exit(1)

    racelist = args.racelist or find_sibling(args.horselist, "racelist")
    payback = args.payback or find_sibling(args.horselist, "payback")

    try:
        run(args.horselist, racelist, payback,
            args.place, args.race, args.top, args.json, args.ev)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        # head等にパイプした場合に途中で切られても正常終了
        sys.stderr.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
