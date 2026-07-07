#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
競馬 勝ち馬予想アプリ「予想チャットくん」 GUI版 v3(中央・地方対応/Windows)
====================================================================
tkinter製のGUIアプリ。標準ライブラリのみで動作します。

起動方法(Windows):
    python keiba_yosou_gui.py
    ※Python(python.org版)が入っていればダブルクリックでも起動できます

使い方:
    1. [出馬表CSVを開く] で *_horselist.csv を選択
       (同じフォルダの *_racelist.csv / *_payback.csv は自動検出)
    2. 競馬場・レースで絞り込み(任意)
    3. [予想実行] → 左のレース一覧をクリックすると予想詳細を表示
    4. 必要なら [テキスト保存] [JSON保存]

予想に使うのはレース前に分かる情報のみです。
horselist内の「着順」「タイム」列は予想には使用しません。
"""

import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


# ============================================================
# 予想エンジン(レース前情報のみ使用)
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
    def fukusho_rate(self):
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
    apprentice: bool = False
    weight: float = 0.0
    weight_diff: float = 0.0
    total_rec: Record = field(default_factory=Record)
    course_rec: Record = field(default_factory=Record)
    dist_rec: Record = field(default_factory=Record)
    turf_l: Record = field(default_factory=Record)   # 芝左成績
    turf_r: Record = field(default_factory=Record)   # 芝右成績
    dirt_l: Record = field(default_factory=Record)   # ダート左成績
    dirt_r: Record = field(default_factory=Record)   # ダート右成績
    ninki: int = 0
    odds: float = 0.0               # 単勝オッズ(取得できた場合のみ)
    last3: list = field(default_factory=list)  # 直近3走の着順(新しい順)
    jockey_affili: str = ""        # 騎手所属(JRA/大井など)
    trainer_affili: str = ""       # 調教師所属
    score: float = 0.0
    reasons: list = field(default_factory=list)
    scratched: bool = False


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
    horses = []
    for r in read_csv_dicts(path):
        place = str(g(r, "競馬場", default="")).strip()
        race_no = to_int(g(r, "レース番号"))
        umaban = to_int(g(r, "馬番"))
        if not place or race_no <= 0 or umaban <= 0:
            continue
        kinryo_raw = str(g(r, "負担重量", default="") or "")
        ninki = to_int(g(r, "人気"))
        horses.append(Horse(
            place=place, race_no=race_no, umaban=umaban,
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
    return horses


def load_racelist(path: Path):
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
    result = {}
    for r in read_csv_dicts(path):
        place = str(g(r, "競馬場", default="")).strip()
        race_no = to_int(g(r, "レース番号"))
        win_uma = to_int(g(r, "単勝組番"))
        pay = to_int(g(r, "単勝払戻金（円）", "単勝払戻金(円)"))
        fuku = [to_int(g(r, f"複勝組番{i}")) for i in (1, 2, 3)]
        fuku = [u for u in fuku if u > 0]
        if place and race_no > 0 and win_uma > 0:
            result[(place, race_no)] = {
                "勝ち馬番": win_uma, "単勝払戻": pay, "複勝馬番": fuku,
            }
    return result


def find_sibling(horselist_path: Path, keyword: str):
    """horselist CSV と同じディレクトリにある racelist / payback を探す。
    ファイル名の 'horselist' 部分だけを keyword に置換して完全一致で探すため、
    以下の命名規則にそのまま対応する(同ディレクトリ内・完全一致):
      - 地方競馬: YYYYMMDD_horselist.csv → YYYYMMDD_payback.csv
      - 中央競馬: JRA_YYYY_MMDD_horselist.csv → JRA_YYYY_MMDD_payback.csv
    """
    cand = Path(str(horselist_path).replace("horselist", keyword))
    if cand != horselist_path and cand.exists():
        return cand
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
    "ninki": 1.0,        # 人気点のスケール
    "kinryo": 200.0,     # 斤量(平均比)の係数
    "softmax_t": 10.0,   # スコア→勝率の温度
    "jra_affili": 8.0,   # JRA所属馬への加点(交流重賞での実力差を考慮)
}


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
    return merge_rec(*pair)  # 直線・不明時は左右合算


def score_horse(h: Horse, info=None):
    score = 50.0
    reasons = []

    tr = h.total_rec
    if tr.starts > 0:
        conf = min(1.0, tr.starts / 10)
        score += (tr.win_rate * PARAMS["total_win"]
                  + tr.fukusho_rate * PARAMS["total_fuku"]) * conf
        if tr.win_rate >= 0.25 and tr.starts >= 5:
            reasons.append(f"通算勝率{tr.win_rate:.0%}の実力馬")
        elif tr.fukusho_rate >= 0.5 and tr.starts >= 5:
            reasons.append(f"複勝率{tr.fukusho_rate:.0%}と堅実")
    else:
        score -= 3
        reasons.append("キャリア浅く未知数")

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

    npt = PARAMS["ninki"]
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

    if h.weight > 0 and h.weight_diff != 0:
        ratio = abs(h.weight_diff) / h.weight
        if ratio >= 0.02:
            score -= 5
            reasons.append(f"馬体重{h.weight_diff:+.0f}kgの大幅変動は不安")
        elif ratio >= 0.012:
            score -= 2

    if h.age >= 10:
        score -= 3
    elif h.age >= 8:
        score -= 1

    h.score = score
    h.reasons = reasons


def adjust_kinryo(horses):
    ks = [h.kinryo for h in horses if h.kinryo > 0]
    if len(ks) < 2:
        return
    avg = sum(ks) / len(ks)
    if avg <= 0:
        return
    for h in horses:
        if h.kinryo > 0:
            adj = -((h.kinryo - avg) / avg) * PARAMS["kinryo"]
            h.score += adj
            if adj >= 3:
                h.reasons.append("ハンデ差(軽量)は大きな武器")
            elif adj <= -4:
                h.reasons.append("重い負担重量がカギ")


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


def predict_race(horses, info=None):
    """1レース分を予想して並べ替えたリストを返す"""
    for h in horses:
        score_horse(h, info)
    adjust_kinryo(horses)
    return sorted(horses, key=lambda x: x.score, reverse=True)


# ============================================================
# 期待値買い(単勝)
# ============================================================
# 考え方:
#   モデルスコア → レース内softmaxで「勝率」を推定
#   人気順位   → 市場支持率(Zipf近似)から「推定オッズ」を算出
#   期待値 = 推定勝率 × 推定オッズ。閾値以上の馬だけ買い、なければ見送り。
# 注意:
#   推定オッズは人気からの概算であり、実オッズとは差があります。
#   期待値100%超は「モデル上の見込み」であって利益の保証ではありません。

SOFTMAX_T = 10.0        # スコア→確率の温度(小さいほど本命に集中)
TAKEOUT_RETURN = 0.75   # 地方競馬 単勝の払戻率(控除率約25%)
TAKEOUT_RETURN_JRA = 0.80  # 中央競馬 単勝の払戻率(控除率20%)
EV_MAX_BETS = 2         # 1レースの最大買い点数
ZIPF_ALPHA = 1.35       # 人気→市場支持率の減衰係数


def model_probs(ranked):
    """モデルスコアから各馬の勝率を推定(softmax)"""
    mx = max(h.score for h in ranked)
    ws = [math.exp((h.score - mx) / PARAMS["softmax_t"]) for h in ranked]
    s = sum(ws)
    return [w / s for w in ws]


def estimate_odds_map(ranked, payout=TAKEOUT_RETURN):
    """単勝オッズマップ {馬番: (オッズ, 実オッズか)}
    実オッズ列があればそれを優先し、無い馬は人気順位から推定する"""
    n = len(ranked)
    raw = {}
    for h in ranked:
        rank = h.ninki if h.ninki > 0 else n
        raw[h.umaban] = rank ** (-ZIPF_ALPHA)
    s = sum(raw.values())
    out = {}
    for h in ranked:
        if h.odds > 1.0:
            out[h.umaban] = (h.odds, True)
        else:
            out[h.umaban] = (max(1.1, payout / (raw[h.umaban] / s)), False)
    return out


def ev_bets(ranked, threshold=100.0, payout=TAKEOUT_RETURN):
    """期待値が閾値(%)以上の単勝買い目リストを返す
    戻り値: [{'horse':Horse,'prob':float,'odds':float,'ev':float}, ...](期待値降順)
    空リスト = 見送り
    """
    probs = model_probs(ranked)
    odds_map = estimate_odds_map(ranked, payout)
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
# GUI
# ============================================================

class KeibaApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🐴 競馬 予想チャットくん(中央・地方対応)")
        self.geometry("1080x680")
        self.minsize(860, 540)

        self.horses = []
        self.race_info = {}
        self.paybacks = {}
        self.predictions = {}   # key -> {ranked, conf, hit, fuku_hit}
        self.horselist_path = None

        self._build_ui()

    # ---------- UI構築 ----------
    def _build_ui(self):
        default_font = ("Meiryo UI", 10) if sys.platform.startswith("win") else ("TkDefaultFont", 10)
        self.option_add("*Font", default_font)

        # --- 上部: ファイル選択 ---
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Button(top, text="📂 出馬表CSVを開く", command=self.open_csv).pack(side="left")
        self.jra_btn = ttk.Button(top, text="🌐 JRAから取得", command=self.fetch_from_jra)
        self.jra_btn.pack(side="left", padx=(6, 0))
        self.nar_btn = ttk.Button(top, text="🏇 地方データ取得", command=self.fetch_from_nar)
        self.nar_btn.pack(side="left", padx=(6, 0))
        self.result_btn = ttk.Button(top, text="🏁 JRA結果取得", command=self.fetch_results_jra)
        self.result_btn.pack(side="left", padx=(6, 0))
        self.tune_btn = ttk.Button(top, text="📈 結果から学習", command=self.tune_from_results,
                                   state="disabled")
        self.tune_btn.pack(side="left", padx=(6, 0))
        self.spat4_btn = ttk.Button(top, text="🎫 SPAT4投票リスト", command=self.make_spat4_list,
                                    state="disabled")
        self.spat4_btn.pack(side="left", padx=(6, 0))
        self.file_label = ttk.Label(top, text="出馬表(*_horselist.csv)を選択してください", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        # --- フィルタ行 ---
        flt = ttk.Frame(self, padding=(10, 0))
        flt.pack(fill="x")
        ttk.Label(flt, text="競馬場:").pack(side="left")
        self.place_var = tk.StringVar(value="すべて")
        self.place_cb = ttk.Combobox(flt, textvariable=self.place_var, width=10,
                                     state="disabled", values=["すべて"])
        self.place_cb.pack(side="left", padx=(4, 12))
        ttk.Label(flt, text="レース:").pack(side="left")
        self.race_var = tk.StringVar(value="すべて")
        self.race_cb = ttk.Combobox(flt, textvariable=self.race_var, width=8,
                                    state="disabled", values=["すべて"])
        self.race_cb.pack(side="left", padx=(4, 12))
        ttk.Label(flt, text="期待値閾値:").pack(side="left")
        self.ev_var = tk.StringVar(value="100")
        ttk.Spinbox(flt, textvariable=self.ev_var, from_=80, to=200,
                    increment=5, width=5).pack(side="left")
        ttk.Label(flt, text="%以上").pack(side="left", padx=(2, 12))
        self.run_btn = ttk.Button(flt, text="🏇 予想実行", command=self.run_prediction, state="disabled")
        self.run_btn.pack(side="left", padx=4)
        self.save_txt_btn = ttk.Button(flt, text="💾 テキスト保存", command=self.save_text, state="disabled")
        self.save_txt_btn.pack(side="left", padx=4)
        self.save_json_btn = ttk.Button(flt, text="💾 JSON保存", command=self.save_json, state="disabled")
        self.save_json_btn.pack(side="left", padx=4)

        # --- 中央: 左レース一覧 / 右チャット表示 ---
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=8)

        left = ttk.Frame(paned)
        cols = ("race", "name", "honmei", "conf", "ev", "result")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        headers = {"race": "レース", "name": "レース名", "honmei": "◎本命",
                   "conf": "信頼度", "ev": "💰買い目", "result": "結果"}
        widths = {"race": 90, "name": 160, "honmei": 140, "conf": 55,
                  "ev": 90, "result": 75}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c],
                             anchor="center" if c in ("race", "conf", "result") else "w")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_race)
        self.tree.tag_configure("hit", background="#e2f5e2")
        self.tree.tag_configure("fuku", background="#fff6dc")
        self.tree.tag_configure("miss", background="#fbe6e6")
        paned.add(left, weight=2)

        right = ttk.Frame(paned)
        mono = ("Meiryo UI", 11) if sys.platform.startswith("win") else ("TkDefaultFont", 11)
        self.text = ScrolledText(right, wrap="word", font=mono,
                                 state="disabled", padx=10, pady=8, background="#fbfaf6")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("h1", font=(mono[0], 13, "bold"), foreground="#1a3b6e")
        self.text.tag_configure("meta", foreground="#777")
        self.text.tag_configure("honmei", font=(mono[0], 12, "bold"), foreground="#b3261e")
        self.text.tag_configure("mark0", foreground="#b3261e", font=(mono[0], 11, "bold"))
        self.text.tag_configure("chat", foreground="#333")
        self.text.tag_configure("kai", foreground="#8a6d00")
        self.text.tag_configure("hit", foreground="#0a7a2f", font=(mono[0], 11, "bold"))
        self.text.tag_configure("miss", foreground="#888")
        paned.add(right, weight=3)

        # --- 下部: ステータスバー ---
        self.status = ttk.Label(self, text="CSVを読み込むと予想できます。予想はあくまで参考です🐴",
                                anchor="w", padding=(10, 4), relief="groove")
        self.status.pack(fill="x", side="bottom")

    # ---------- ファイル読み込み ----------
    def open_csv(self):
        path = filedialog.askopenfilename(
            title="出馬表CSV(*_horselist.csv)を選択",
            filetypes=[("CSVファイル", "*.csv"), ("すべてのファイル", "*.*")])
        if not path:
            return
        self.load_horselist_file(Path(path))

    def load_horselist_file(self, path: Path):
        try:
            self.horses = load_horselist(path)
        except Exception as e:
            messagebox.showerror("読み込みエラー", str(e))
            return
        self.horselist_path = path

        rl = find_sibling(path, "racelist")
        pb = find_sibling(path, "payback")
        self.race_info = load_racelist(rl) if rl else {}
        self.paybacks = load_payback(pb) if pb else {}

        n_races = len({(h.place, h.race_no) for h in self.horses if not h.scratched})
        extras = []
        if rl:
            extras.append(f"レース情報: {rl.name}")
        if pb:
            extras.append(f"払戻: {pb.name}")
        extra_s = " / ".join(extras) if extras else "racelist・payback未検出(予想のみ)"
        self.file_label.config(
            text=f"{path.name}({len(self.horses)}頭・{n_races}レース) | {extra_s}",
            foreground="#000")

        places = sorted({h.place for h in self.horses})
        race_nos = sorted({h.race_no for h in self.horses})
        self.place_cb.config(values=["すべて"] + places, state="readonly")
        self.race_cb.config(values=["すべて"] + [str(n) for n in race_nos], state="readonly")
        self.place_var.set("すべて")
        self.race_var.set("すべて")
        self.run_btn.config(state="normal")
        self.tune_btn.config(state="normal" if self.paybacks else "disabled")
        self.spat4_btn.config(state="normal")
        jra_p = [pl for pl in places if is_jra(pl)]
        nar_p = [pl for pl in places if not is_jra(pl)]
        parts = []
        if jra_p:
            parts.append(f"中央: {'、'.join(jra_p)}")
        if nar_p:
            parts.append(f"地方: {'、'.join(nar_p)}")
        self.status.config(text=f"読み込み完了({' / '.join(parts)})。[予想実行]を押してね!")

    # ---------- JRAから取得 ----------
    def fetch_from_jra(self):
        try:
            import fetch_jra
        except ImportError:
            messagebox.showerror(
                "モジュールなし",
                "fetch_jra.py が見つかりません。\n"
                "keiba_yosou_gui.py と同じフォルダに置いてください。")
            return
        if not messagebox.askokcancel(
                "JRAから取得",
                "JRA公式サイトから今週の出馬表を取得します。\n\n"
                "・個人利用の範囲で、JRAの利用規約をご確認ください\n"
                "・サーバー負荷防止のため1レースごとに1.5秒待機します\n"
                "  (全レースで数分かかります)\n"
                "・サイト構造変更により失敗する場合があります\n\n"
                "取得を開始しますか?"):
            return
        out_dir = filedialog.askdirectory(title="CSVの保存先フォルダを選択")
        if not out_dir:
            return

        self.jra_btn.config(state="disabled")
        self._log_clear()
        self._log("🌐 JRAから取得を開始します...\n")

        import threading

        def worker():
            try:
                h_path, r_path, n = fetch_jra.fetch_thisweek(
                    out_dir, progress=lambda m: self.after(0, self._log, m + "\n"))
                self.after(0, self._fetch_done, h_path, n)
            except Exception as e:
                self.after(0, self._fetch_failed, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _log_clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def _log(self, msg):
        self.text.config(state="normal")
        self.text.insert("end", msg, "chat")
        self.text.see("end")
        self.text.config(state="disabled")

    def _fetch_done(self, h_path, n):
        self.jra_btn.config(state="normal")
        self._log(f"\n✅ 取得完了({n}レース)。読み込んで予想できます!\n")
        self.load_horselist_file(Path(h_path))

    def _fetch_failed(self, msg):
        self.jra_btn.config(state="normal")
        self._log(f"\n❌ 取得失敗: {msg}\n")
        messagebox.showerror(
            "取得失敗",
            f"{msg}\n\nJRAサイトの構造変更や通信環境が原因の可能性があります。\n"
            "fetch_jra.py を --debug 付きで単体実行すると調査用HTMLを保存できます。")

    # ---------- 地方データ取得(keiba.go.jp) ----------
    def fetch_from_nar(self):
        try:
            import download_race_data
        except ImportError:
            messagebox.showerror(
                "モジュールなし",
                "download_race_data.py が見つかりません。\n"
                "keiba_yosou_gui.py と同じフォルダに置いてください。")
            return
        if not messagebox.askokcancel(
                "地方競馬データ取得",
                "地方競馬情報サイト(keiba.go.jp)から本日の出走データを\n"
                "ダウンロード・解凍して読み込みます。\n\n"
                "・個人利用の範囲で、サイトの利用条件をご確認ください\n"
                "・取得先の仕様変更で失敗する場合があります\n\n取得を開始しますか?"):
            return
        self.nar_btn.config(state="disabled")
        self._log_clear()
        self._log("🏇 地方競馬データを取得します...\n")

        import threading
        from datetime import date as _date

        def worker():
            try:
                hl = download_race_data.fetch_nar(
                    _date.today(),
                    progress=lambda m: self.after(0, self._log, str(m) + "\n"))
                self.after(0, self._nar_done, hl)
            except Exception as e:
                self.after(0, self._nar_failed, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _nar_done(self, hl):
        self.nar_btn.config(state="normal")
        if hl is None:
            self._log("\n⚠ 出馬表CSVが見つかりませんでした。解凍先フォルダを確認してください。\n")
            messagebox.showwarning(
                "CSV未検出",
                "ダウンロード・解凍は完了しましたが、horselist CSVを特定できませんでした。\n"
                "[出馬表CSVを開く]から手動で選択してください。")
            return
        self._log(f"\n✅ 読み込みます: {Path(hl).name}\n")
        self.load_horselist_file(Path(hl))

    def _nar_failed(self, msg):
        self.nar_btn.config(state="normal")
        self._log(f"\n❌ 取得失敗: {msg}\n")
        messagebox.showerror("取得失敗", msg)

    # ---------- JRA結果取得 ----------
    def fetch_results_jra(self):
        try:
            import fetch_jra
        except ImportError:
            messagebox.showerror("モジュールなし", "fetch_jra.py を同じフォルダに置いてください。")
            return
        if not messagebox.askokcancel(
                "JRA結果取得",
                "JRA公式サイトから本日のレース結果(着順・払戻)を取得します。\n"
                "個人利用の範囲で、利用規約をご確認ください。\n取得を開始しますか?"):
            return
        out_dir = (self.horselist_path.parent if self.horselist_path
                   else filedialog.askdirectory(title="CSVの保存先フォルダを選択"))
        if not out_dir:
            return
        self.result_btn.config(state="disabled")
        self._log_clear()
        self._log("🏁 JRAから本日の結果を取得します...\n")

        import threading

        def worker():
            try:
                path, n = fetch_jra.fetch_results(
                    out_dir, progress=lambda m: self.after(0, self._log, m + "\n"))
                self.after(0, self._results_done, path, n)
            except Exception as e:
                self.after(0, self._results_failed, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _results_done(self, path, n):
        self.result_btn.config(state="normal")
        self._log(f"\n✅ 結果{n}レース分を取得: {Path(path).name}\n")
        try:
            self.paybacks = load_payback(Path(path))
        except Exception as e:
            self._log(f"⚠ 払戻の読み込みに失敗: {e}\n")
            return
        if self.horses:
            self.tune_btn.config(state="normal")
            self._log("答え合わせ付きで再予想します...\n")
            self.run_prediction()
        else:
            self._log("出馬表CSVを開くと答え合わせできます。\n")

    def _results_failed(self, msg):
        self.result_btn.config(state="normal")
        self._log(f"\n❌ 結果取得失敗: {msg}\n")
        messagebox.showerror("取得失敗", msg)

    # ---------- 結果から学習(パラメータ調整) ----------
    def tune_from_results(self):
        if not self.horselist_path or not self.paybacks:
            messagebox.showinfo("学習できません", "出馬表と結果(payback)の両方が必要です。")
            return
        try:
            import tune_params
        except ImportError:
            messagebox.showerror("モジュールなし",
                                 "tune_params.py と keiba_yosou.py を同じフォルダに置いてください。")
            return
        if not messagebox.askokcancel(
                "結果から学習",
                "本日の予想と結果の差異をもとに、予想パラメータを自動調整して\n"
                "keiba_params.json に保存します(次回以降も自動で使用)。\n\n"
                "⚠ 1日分だけの調整は過学習しやすく、翌週の成績を保証しません。\n"
                "複数週のデータを貯めてからの調整がおすすめです。\n\n実行しますか?"):
            return
        self.tune_btn.config(state="disabled")
        self._log_clear()
        self._log("📈 結果から学習中(数十秒かかることがあります)...\n")

        import threading
        hl = self.horselist_path

        def worker():
            try:
                best_p, base, best_r, n = tune_params.tune(
                    [str(hl)], iters=400,
                    progress=lambda m: self.after(0, self._log, str(m) + "\n"))
                meta = {"学習レース数": n, "調整前_的中": base[0], "調整後_的中": best_r[0],
                        "注意": "学習データ上の成績。未来の的中を保証しません"}
                path = tune_params.save_params(best_p, meta)
                self.after(0, self._tune_done, path)
            except Exception as e:
                self.after(0, self._tune_failed, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _tune_done(self, path):
        self.tune_btn.config(state="normal")
        load_params()  # GUI側エンジンにも即反映
        self._log(f"\n💾 保存: {Path(path).name}(次回起動時も自動で使用)\n")
        self._log("新しいパラメータで再予想します...\n")
        self.run_prediction()

    def _tune_failed(self, msg):
        self.tune_btn.config(state="normal")
        self._log(f"\n❌ 学習失敗: {msg}\n")
        messagebox.showerror("学習失敗", msg)

    # ---------- SPAT4投票リスト ----------
    def make_spat4_list(self):
        if not self.horselist_path:
            messagebox.showinfo("先にCSVを開いてください", "出馬表CSVを読み込んでから実行してください。")
            return
        try:
            import spat4_bet
        except ImportError:
            messagebox.showerror("モジュールなし",
                                 "spat4_bet.py と keiba_yosou.py を同じフォルダに置いてください。")
            return
        dlg = _Spat4Dialog(self)
        self.wait_window(dlg)
        if not dlg.result:
            return
        p = dlg.result
        try:
            ymd = spat4_bet.detect_ymd(self.horselist_path,
                                       __import__("datetime").date.today().strftime("%Y%m%d"))
            txt, csvp, n, total = spat4_bet.run(
                self.horselist_path, p["ev"], p["mode"], p["unit"],
                p["budget"], p["bankroll"], p["kelly_frac"],
                self.horselist_path.parent, ymd,
                progress=lambda m: None)
        except Exception as e:
            messagebox.showerror("生成失敗", str(e))
            return
        self._log_clear()
        self._log(Path(txt).read_text(encoding="utf-8") + "\n")
        messagebox.showinfo(
            "投票リストを出力しました",
            f"投票{n}点 / 合計{total:,}円\n\n"
            f"{Path(txt).name}\n{Path(csvp).name}\n"
            f"(場所: {Path(txt).parent})\n\n"
            "⚠ これは投票の下書きです。SPAT4サイトでご自身でログイン・確認・"
            "投票してください。認証情報の自動入力や自動購入は行いません。")

    # ---------- 予想実行 ----------
    def run_prediction(self):
        place_f = self.place_var.get()
        race_f = self.race_var.get()

        races = {}
        for h in self.horses:
            if h.scratched:
                continue
            if place_f != "すべて" and h.place != place_f:
                continue
            if race_f != "すべて" and h.race_no != int(race_f):
                continue
            races.setdefault((h.place, h.race_no), []).append(h)

        if not races:
            messagebox.showinfo("該当なし", "条件に合うレースがありません。絞り込みを見直してください。")
            return

        try:
            ev_threshold = float(self.ev_var.get())
        except ValueError:
            ev_threshold = 100.0
            self.ev_var.set("100")

        self.predictions = {}
        self.tree.delete(*self.tree.get_children())

        hits = fuku_hits = graded = ret = 0
        ev_bets_n = ev_hits = ev_invest = ev_ret = ev_skip = 0
        for key in sorted(races, key=lambda k: (k[0], k[1])):
            info = self.race_info.get(key, {})
            jra = is_jra(key[0])
            ranked = predict_race(races[key], info)
            conf_text, conf_rank = confidence_label(ranked)
            payout = TAKEOUT_RETURN_JRA if jra else TAKEOUT_RETURN
            picks = ev_bets(ranked, ev_threshold, payout)
            ev_s = ("単勝 " + ",".join(str(p["horse"].umaban) for p in picks)) if picks else "見送り"
            pb = self.paybacks.get(key)
            hit = fuku_hit = None
            result_s = "－"
            tag = ()
            if pb:
                hit = ranked[0].umaban == pb["勝ち馬番"]
                fuku_hit = ranked[0].umaban in pb.get("複勝馬番", [])
                graded += 1
                if hit:
                    hits += 1
                    fuku_hits += 1
                    ret += pb["単勝払戻"]
                    result_s, tag = "◎的中!", ("hit",)
                elif fuku_hit:
                    fuku_hits += 1
                    result_s, tag = "複勝圏", ("fuku",)
                else:
                    result_s, tag = "外れ", ("miss",)
                # 期待値買いのバックテスト
                if not picks:
                    ev_skip += 1
                for p in picks:
                    ev_bets_n += 1
                    ev_invest += 100
                    if p["horse"].umaban == pb["勝ち馬番"]:
                        ev_hits += 1
                        ev_ret += pb["単勝払戻"]
            self.predictions[key] = {
                "ranked": ranked, "conf": (conf_text, conf_rank),
                "payback": pb, "hit": hit, "fuku_hit": fuku_hit,
                "ev_picks": picks, "ev_threshold": ev_threshold,
                "jra": jra,
            }
            self.tree.insert("", "end", iid=f"{key[0]}|{key[1]}",
                             values=(f"{'🏛' if jra else ''}{key[0]} {key[1]}R",
                                     info.get("レース名", ""),
                                     f"{ranked[0].umaban}番 {ranked[0].name}",
                                     conf_rank, ev_s, result_s),
                             tags=tag)

        n = len(self.predictions)
        if graded:
            ev_recov = ev_ret / ev_invest * 100 if ev_invest else 0
            self.status.config(
                text=f"予想完了: {n}レース | 終了{graded}Rの答え合わせ → "
                     f"◎単勝{hits}/{graded}・3着内{fuku_hits}/{graded} | "
                     f"💰期待値買い(閾値{ev_threshold:.0f}%): {ev_bets_n}点購入(見送り{ev_skip}R)・"
                     f"的中{ev_hits}・投資{ev_invest}円→払戻{ev_ret}円(回収率{ev_recov:.0f}%) | 参考予想です🐴")
        else:
            self.status.config(text=f"予想完了: {n}レース。一覧をクリックすると詳細を表示します | 参考予想です🐴")

        self.save_txt_btn.config(state="normal")
        self.save_json_btn.config(state="normal")

        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.see(first[0])

    # ---------- 詳細表示 ----------
    def on_select_race(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        place, race_no = sel[0].split("|")
        key = (place, int(race_no))
        p = self.predictions.get(key)
        if not p:
            return

        ranked = p["ranked"]
        conf_text, conf_rank = p["conf"]
        info = self.race_info.get(key, {})
        top = ranked[0]

        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")

        mode = "中央" if p.get("jra") else "地方"
        header = f"🏇 【{mode}】{place} 第{race_no}レース"
        if info.get("レース名"):
            header += f"「{info['レース名']}」"
        t.insert("end", header + "\n", "h1")
        meta = []
        if info.get("発走時刻"):
            meta.append(f"発走{info['発走時刻']}")
        if info.get("芝ダート") and info.get("距離"):
            mawari = info.get("回り", "")
            meta.append(f"{info['芝ダート']}{'(' + mawari + '回り)' if mawari and mawari != '直' else ''}{info['距離']}m")
        if info.get("頭数"):
            meta.append(f"{info['頭数']}頭")
        if info.get("天候"):
            meta.append(f"天候:{info['天候']}")
        if meta:
            t.insert("end", " / ".join(meta) + "\n", "meta")
        t.insert("end", "\n")

        t.insert("end", f"💬 本命は ◎ {top.umaban}番【{top.name}】"
                        f"(信頼度{conf_rank}:{conf_text})\n", "honmei")
        sub = []
        if top.ninki > 0:
            sub.append(f"{top.ninki}番人気")
        if top.jockey:
            sub.append(f"{top.jockey}騎手")
        if sub:
            t.insert("end", "   " + " / ".join(sub) + "\n", "chat")
        for r in top.reasons[:3] or ["総合力で一枚上と見た!"]:
            t.insert("end", f"   ・{r}\n", "chat")
        t.insert("end", "\n📋 印まとめ:\n", "chat")
        for i, h in enumerate(ranked):
            mark = MARKS[i] if i < len(MARKS) else "  "
            ninki_s = f"{h.ninki}人気" if h.ninki > 0 else "-"
            line = (f" {mark} {h.umaban:>2}番 {h.name:<12} "
                    f"スコア{h.score:6.1f} ({ninki_s}/{h.jockey})\n")
            t.insert("end", line, "mark0" if i == 0 else "chat")

        if len(ranked) >= 3:
            a, b, c = ranked[0].umaban, ranked[1].umaban, ranked[2].umaban
            t.insert("end", f"\n🎯 参考買い目: 単勝 {a} / 馬複 {a}-{b} / 三連複 {a}-{b}-{c}\n", "kai")
        if conf_rank == "C":
            t.insert("end", "💬 このレースは混戦模様。手広く構えるのが吉かも!\n", "chat")

        # --- 期待値買い ---
        picks = p.get("ev_picks", [])
        thr = p.get("ev_threshold", 100)
        t.insert("end", f"\n💰 期待値買い(単勝・閾値{thr:.0f}%):\n", "honmei")
        if picks:
            for pk in picks:
                h = pk["horse"]
                o_label = "実オッズ" if pk.get("real") else "推定オッズ"
                t.insert("end",
                         f"   {h.umaban}番 {h.name}  推定勝率{pk['prob']:.0%} × "
                         f"{o_label}{pk['odds']:.1f}倍 = 期待値{pk['ev']:.0f}%\n", "kai")
            if all(pk.get("real") for pk in picks):
                t.insert("end", "   ※取得時点の実オッズで計算。オッズは締切まで変動するよ\n", "meta")
            else:
                t.insert("end", "   ※推定オッズは人気からの概算。買う直前に実オッズと見比べてね\n", "meta")
        else:
            t.insert("end", "   このレースは期待値が閾値に届かず【見送り】。\n"
                            "   買わない判断も回収率アップの立派な戦略だよ!\n", "chat")

        pb = p["payback"]
        if pb:
            win = pb["勝ち馬番"]
            win_h = next((h for h in ranked if h.umaban == win), None)
            win_name = f"【{win_h.name}】" if win_h else ""
            t.insert("end", "\n")
            if p["hit"]:
                t.insert("end", f"✅ 結果: {win}番{win_name}が勝利! "
                                f"◎的中🎉 (単勝{pb['単勝払戻']}円)\n", "hit")
            elif p["fuku_hit"]:
                rank = next((i + 1 for i, h in enumerate(ranked) if h.umaban == win), None)
                t.insert("end", f"🔶 結果: 勝ったのは{win}番{win_name}"
                                f"(予想{rank}位評価)。◎は3着以内で複勝圏キープ!\n", "kai")
            else:
                rank = next((i + 1 for i, h in enumerate(ranked) if h.umaban == win), None)
                rank_s = f"(予想{rank}位評価)" if rank else ""
                t.insert("end", f"❌ 結果: 勝ったのは{win}番{win_name}{rank_s}。"
                                f"◎は外れ…次いくよ!\n", "miss")

        t.insert("end", "\n※あくまで参考予想です。馬券は余裕資金の範囲で🐴\n", "meta")
        t.config(state="disabled")

    # ---------- 保存 ----------
    def _default_name(self, ext):
        base = self.horselist_path.stem.replace("_horselist", "") if self.horselist_path else "yosou"
        return f"予想結果_{base}.{ext}"

    def save_text(self):
        if not self.predictions:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile=self._default_name("txt"),
            filetypes=[("テキスト", "*.txt")])
        if not path:
            return
        lines = []
        for key in sorted(self.predictions, key=lambda k: (k[0], k[1])):
            p = self.predictions[key]
            ranked = p["ranked"]
            conf_text, conf_rank = p["conf"]
            info = self.race_info.get(key, {})
            lines.append("─" * 50)
            head = f"{key[0]} 第{key[1]}レース"
            if info.get("レース名"):
                head += f"「{info['レース名']}」"
            lines.append(head)
            lines.append(f"本命 ◎{ranked[0].umaban}番 {ranked[0].name}"
                         f"(信頼度{conf_rank}:{conf_text})")
            for i, h in enumerate(ranked[:5]):
                mark = MARKS[i] if i < len(MARKS) else " "
                lines.append(f"  {mark} {h.umaban:>2}番 {h.name} "
                             f"スコア{h.score:.1f} ({h.ninki}人気/{h.jockey})")
            picks = p.get("ev_picks", [])
            if picks:
                pick_s = " / ".join(
                    f"{pk['horse'].umaban}番(勝率{pk['prob']:.0%}×推定{pk['odds']:.1f}倍=期待値{pk['ev']:.0f}%)"
                    for pk in picks)
                lines.append(f"  💰期待値買い(単勝): {pick_s}")
            else:
                lines.append("  💰期待値買い: 見送り")
            if p["payback"]:
                mark = "◎的中!" if p["hit"] else ("複勝圏" if p["fuku_hit"] else "外れ")
                lines.append(f"  結果: 勝ち馬{p['payback']['勝ち馬番']}番 → {mark}")
        lines.append("─" * 50)
        lines.append("※あくまで参考予想です。馬券は余裕資金の範囲で。")
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        self.status.config(text=f"テキスト保存しました: {path}")

    def save_json(self):
        if not self.predictions:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile=self._default_name("json"),
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        out = {}
        for key in sorted(self.predictions, key=lambda k: (k[0], k[1])):
            p = self.predictions[key]
            pb = p["payback"]
            out[f"{key[0]}_{key[1]}R"] = {
                "レース名": self.race_info.get(key, {}).get("レース名", ""),
                "期待値買い_単勝": [
                    {"馬番": pk["horse"].umaban, "馬名": pk["horse"].name,
                     "推定勝率": round(pk["prob"], 3),
                     "推定オッズ": round(pk["odds"], 1),
                     "期待値pct": round(pk["ev"], 1)}
                    for pk in p.get("ev_picks", [])
                ] or "見送り",
                "予想": [
                    {"順位": i + 1, "印": MARKS[i] if i < len(MARKS) else "",
                     "馬番": h.umaban, "馬名": h.name, "騎手": h.jockey,
                     "人気": h.ninki, "スコア": round(h.score, 1), "根拠": h.reasons}
                    for i, h in enumerate(p["ranked"])
                ],
                "結果": ({"勝ち馬番": pb["勝ち馬番"], "単勝払戻": pb["単勝払戻"],
                          "本命的中": p["hit"], "本命3着以内": p["fuku_hit"]} if pb else None),
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        self.status.config(text=f"JSON保存しました: {path}")


class _Spat4Dialog(tk.Toplevel):
    """SPAT4投票リストの条件入力ダイアログ"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("SPAT4投票リストの条件")
        self.result = None
        self.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="期待値しきい値(%以上)").grid(row=0, column=0, sticky="w", **pad)
        self.ev = tk.StringVar(value="130")
        ttk.Spinbox(frm, textvariable=self.ev, from_=100, to=1000, increment=10,
                    width=8).grid(row=0, column=1, **pad)

        ttk.Label(frm, text="資金配分").grid(row=1, column=0, sticky="w", **pad)
        self.mode = tk.StringVar(value="flat")
        mf = ttk.Frame(frm)
        mf.grid(row=1, column=1, sticky="w", **pad)
        ttk.Radiobutton(mf, text="定額", variable=self.mode, value="flat",
                        command=self._toggle).pack(side="left")
        ttk.Radiobutton(mf, text="ケリー", variable=self.mode, value="kelly",
                        command=self._toggle).pack(side="left")

        ttk.Label(frm, text="1点あたり金額(定額)").grid(row=2, column=0, sticky="w", **pad)
        self.unit = tk.StringVar(value="100")
        self.unit_sp = ttk.Spinbox(frm, textvariable=self.unit, from_=100, to=10000,
                                   increment=100, width=8)
        self.unit_sp.grid(row=2, column=1, **pad)

        ttk.Label(frm, text="1日の上限予算(円)").grid(row=3, column=0, sticky="w", **pad)
        self.budget = tk.StringVar(value="3000")
        ttk.Spinbox(frm, textvariable=self.budget, from_=0, to=1000000,
                    increment=1000, width=8).grid(row=3, column=1, **pad)

        ttk.Label(frm, text="総資金(ケリー時, 円)").grid(row=4, column=0, sticky="w", **pad)
        self.bankroll = tk.StringVar(value="30000")
        self.bank_sp = ttk.Spinbox(frm, textvariable=self.bankroll, from_=1000,
                                   to=10000000, increment=5000, width=8, state="disabled")
        self.bank_sp.grid(row=4, column=1, **pad)

        note = ("⚠ これは投票の下書きを作る機能です。ログイン・購入の確定は\n"
                "SPAT4サイトでご自身が行います。認証情報の自動入力や自動購入は\n"
                "行いません。馬券は20歳以上・自己責任で。")
        ttk.Label(frm, text=note, foreground="#a05a00",
                  justify="left").grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        bf = ttk.Frame(frm)
        bf.grid(row=6, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(bf, text="リスト作成", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bf, text="キャンセル", command=self.destroy).pack(side="left", padx=6)
        self.transient(parent)
        self.grab_set()

    def _toggle(self):
        kelly = self.mode.get() == "kelly"
        self.bank_sp.config(state="normal" if kelly else "disabled")
        self.unit_sp.config(state="disabled" if kelly else "normal")

    def _ok(self):
        def num(v, d):
            try:
                return type(d)(float(v))
            except ValueError:
                return d
        self.result = {
            "ev": num(self.ev.get(), 130.0),
            "mode": self.mode.get(),
            "unit": num(self.unit.get(), 100),
            "budget": num(self.budget.get(), 0),
            "bankroll": num(self.bankroll.get(), 30000),
            "kelly_frac": 0.25,
        }
        self.destroy()


def main():
    app = KeibaApp()
    app.mainloop()


if __name__ == "__main__":
    main()
