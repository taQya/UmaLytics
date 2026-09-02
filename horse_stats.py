#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""horses.sqlite3(horse_db.pyが蓄積した馬の生涯レース履歴)を予想スコアに使う。
=====================================================================
deba_table.py は地方競馬の出馬表ページから「直近5走」しか取れないのに対し、
horses.sqlite3 には馬ごとの生涯全レース履歴(JRA交流分を含む)が蓄積されて
いる。ここではその生涯履歴から、CSV/出馬表だけでは取れない4特徴量を追加
する(どれも「レース前に分かる情報」のみ・予想対象日より前のレースのみ集計)。

  db_recent   : 生涯レースを日付ベースで指数重み付けした近走点。
                deba/CSVの直近3走より長いレンジ・多いレース数で今の調子を見る。
  db_dist     : 出走競馬場を問わない当距離適性(CSVの「うち当距離成績」は
                出走中の競馬場限定なので、転入・転厩直後の馬には効かない)。
  db_jockey   : 「この馬×今回の騎手」のコンビ成績(騎手全体の勝率とは別に、
                この組み合わせ固有の相性を見る)。
  db_interval : 前走からの間隔。出馬表キャッシュ(deba)が無い馬にも使える。

馬の特定は出馬表キャッシュの血統登録番号を優先し、無ければ馬名の完全一致
(DB内で一意な場合のみ)で引く。DBが無い/馬が見つからない場合は何もせず、
既存の予想結果はそのまま(壊さない)。

取得したレース履歴は Horse.db にキャッシュし、同じ馬を何度予想し直しても
(tune_params.py のパラメータ探索など)DBへは1回しか問い合わせない。
"""

import sqlite3
from datetime import date
from pathlib import Path


def _default_db_path():
    import sys as _sys
    cands = []
    if getattr(_sys, "frozen", False):        # exe実行時
        cands.append(Path(_sys.executable).parent / "horses.sqlite3")
    cands.append(Path(__file__).resolve().parent / "horses.sqlite3")
    cands.append(Path.cwd() / "horses.sqlite3")
    for p in cands:
        if p.exists():
            return p
    return cands[0]


def connect_ro(db_path=None):
    """読み取り専用で接続する。DBファイルが無ければ None を返す。"""
    p = Path(db_path) if db_path else _default_db_path()
    if not p.exists():
        return None
    try:
        return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


# ============================================================
# 馬の特定(血統登録番号の解決)
# ============================================================

def _resolve_codes(con, horses):
    """{id(horse): lineage_code} を返す。出馬表キャッシュ優先、無ければ馬名の一意一致。"""
    out = {}
    need_name = []
    for h in horses:
        code = (getattr(h, "deba", None) or {}).get("血統登録番号")
        if code:
            out[id(h)] = code
        elif getattr(h, "name", None):
            need_name.append(h)
    if need_name:
        names = sorted({h.name for h in need_name})
        qmarks = ",".join("?" * len(names))
        cur = con.execute(
            f"SELECT name, lineage_code FROM horses WHERE name IN ({qmarks})", names)
        by_name = {}
        for name, code in cur.fetchall():
            by_name.setdefault(name, []).append(code)
        for h in need_name:
            cands = by_name.get(h.name)
            if cands and len(cands) == 1:
                out[id(h)] = cands[0]
    return out


# ============================================================
# 履歴取得(予想対象日より前のみ = 未来情報を混ぜない)
# ============================================================

def _to_iso(ymd):
    """'20260810' / '2026-08-10' → '2026-08-10'。判別できなければ None"""
    s = "".join(ch for ch in str(ymd) if ch.isdigit())
    if len(s) != 8:
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


_HIST_COLS = ("race_date", "track", "distance", "chaku", "time_sec",
              "agari3f", "jockey")


def _fetch_history(con, codes, before_iso):
    codes = sorted(set(codes))
    if not codes or not before_iso:
        return {}
    qmarks = ",".join("?" * len(codes))
    cur = con.execute(
        f"SELECT lineage_code, {', '.join(_HIST_COLS)} FROM races "
        f"WHERE lineage_code IN ({qmarks}) AND race_date < ? "
        f"ORDER BY race_date DESC",
        (*codes, before_iso))
    out = {}
    for row in cur.fetchall():
        code = row[0]
        out.setdefault(code, []).append(dict(zip(_HIST_COLS, row[1:])))
    return out


def ensure_history(horses, db_path=None):
    """未取得の馬だけ horses.sqlite3 から生涯レース履歴を取得し h.db にキャッシュする。
    h.db は一度取得したら再利用するので、同じ馬インスタンスを何度予想しても
    (パラメータ探索など)DBへの問い合わせは初回の1回だけで済む。"""
    todo = [h for h in horses if not getattr(h, "db", None)]
    if not todo:
        return
    con = connect_ro(db_path)
    if con is None:
        for h in todo:
            h.db = {"fetched": True, "code": None, "races": []}
        return
    try:
        id2code = _resolve_codes(con, todo)
        by_before = {}
        for h in todo:
            code = id2code.get(id(h))
            before = _to_iso(getattr(h, "race_date", "") or "")
            if not code or not before:
                h.db = {"fetched": True, "code": code, "races": []}
                continue
            by_before.setdefault(before, []).append((h, code))
        for before, items in by_before.items():
            hist = _fetch_history(con, [c for _, c in items], before)
            for h, code in items:
                h.db = {"fetched": True, "code": code, "races": hist.get(code, [])}
    finally:
        con.close()


# ============================================================
# 特徴量
# ============================================================

_RANK_PTS = {1: 8.0, 2: 5.0, 3: 3.0, 4: 1.0, 5: 1.0}


def _rank_pts(chaku):
    if chaku is None:
        return None
    if chaku in _RANK_PTS:
        return _RANK_PTS[chaku]
    return -1.0 if chaku <= 9 else -2.5


def _days_between(later_iso, earlier_iso):
    y1, m1, d1 = (int(x) for x in later_iso.split("-"))
    y2, m2, d2 = (int(x) for x in earlier_iso.split("-"))
    return (date(y1, m1, d1) - date(y2, m2, d2)).days


def _recent_score(races, before_iso, half_life=45, max_n=10):
    """日数ベースで指数重み付けした近走点(target: 概ね-0.6〜2.0)。
    half_life日で過去レースの重みが半分になる(deba_recentより長いレンジ・
    多いレース数で「今の調子」を捉える)。"""
    pts = wsum = 0.0
    for r in races[:max_n]:
        p = _rank_pts(r["chaku"])
        if p is None or not r["race_date"]:
            continue
        days = max(0, _days_between(before_iso, r["race_date"]))
        w = 0.5 ** (days / half_life)
        pts += p * w
        wsum += w
    return (pts / wsum / 4.0) if wsum >= 1.0 else None


def _distance_aptitude(races, target_dist, tol=0.15):
    """出走競馬場を問わない当距離(±tol)適性"""
    if not target_dist:
        return None
    lo, hi = target_dist * (1 - tol), target_dist * (1 + tol)
    sub = [r for r in races
           if r["distance"] and lo <= r["distance"] <= hi and r["chaku"] is not None]
    if len(sub) < 4:
        return None
    starts = len(sub)
    win = sum(1 for r in sub if r["chaku"] == 1)
    fuku = sum(1 for r in sub if r["chaku"] <= 3)
    return {"starts": starts, "win_rate": win / starts, "fuku_rate": fuku / starts}


def _jockey_combo(races, jockey):
    """この馬×指定騎手のコンビ成績(生涯)"""
    if not jockey:
        return None
    sub = [r for r in races if r["jockey"] == jockey and r["chaku"] is not None]
    if len(sub) < 3:
        return None
    starts = len(sub)
    win = sum(1 for r in sub if r["chaku"] == 1)
    fuku = sum(1 for r in sub if r["chaku"] <= 3)
    return {"starts": starts, "win_rate": win / starts, "fuku_rate": fuku / starts}


def _interval_pts(days):
    """レース間隔の評価(deba_table._interval_ptsと同じ考え方: 地方は中2〜3週が標準)"""
    if days is None:
        return 0.0
    if days <= 7:
        return -0.3
    if days <= 28:
        return 1.0
    if days <= 60:
        return 0.3
    if days <= 120:
        return -0.5
    return -1.0


def _reasons(dist, combo, params):
    out = []
    if params.get("db_dist") and dist and dist["win_rate"] >= 0.25 and dist["starts"] >= 5:
        out.append(f"全競馬場通算でこの距離{dist['starts']}戦{dist['win_rate']:.0%}と好相性")
    if params.get("db_jockey") and combo and combo["win_rate"] >= 0.3 and combo["starts"] >= 3:
        out.append(f"この騎手とは{combo['starts']}戦{combo['win_rate']:.0%}とコンビ実績あり")
    return out


# ============================================================
# エンジンへの適用
# ============================================================

# 出馬表ページ由来(deba_table.PARAMS_DEFAULT)と同様に、エンジン側の PARAMS に
# この初期値がマージされる。tune_params.py で調整できる。
PARAMS_DEFAULT = {
    "db_recent":   0.4,   # 生涯レース履歴の近走点(日数ベース指数重み)
    "db_dist":     0.3,   # 全競馬場通算の当距離適性
    "db_jockey":   0.2,   # この馬×今回騎手のコンビ成績
    "db_interval": 0.2,   # 前走(DB上の最新レース)からの間隔
}


def _ensure_features(h, target_dist):
    """h.db["races"](生涯レース履歴)から特徴量を計算し h.db["feat"] にキャッシュする。
    特徴量自体はパラメータに依存しないので、パラメータ探索(tune_params.py)で
    同じ馬を何度スコアリングし直しても履歴の再走査は初回の1回だけで済む。"""
    d = h.db
    if "feat" in d:
        return d["feat"]
    races = d.get("races") or []
    before = _to_iso(getattr(h, "race_date", "") or "")
    feat = {}
    if races and before:
        feat = {
            "recent": _recent_score(races, before),
            "dist": _distance_aptitude(races, target_dist),
            "combo": _jockey_combo(races, getattr(h, "jockey", "")),
            "interval": (_days_between(before, races[0]["race_date"])
                         if races[0].get("race_date") else None),
        }
    d["feat"] = feat
    return feat


def adjust(horses, info, params):
    """horses.sqlite3由来の特徴量でスコアを補正する(レース単位で呼ぶ)。
    DBが無い/紐付かない馬はスコアを変えない(既存の予想を壊さない)。"""
    ensure_history(horses)
    target_dist = (info or {}).get("距離") or 0
    for h in horses:
        if not getattr(h, "db", None):
            continue
        feat = _ensure_features(h, target_dist)
        if not feat:
            continue

        if feat.get("recent") is not None:
            h.score += params.get("db_recent", 0.0) * feat["recent"]

        dist = feat.get("dist")
        if dist:
            h.score += ((dist["win_rate"] * 15 + dist["fuku_rate"] * 8)
                        * params.get("db_dist", 0.0))

        combo = feat.get("combo")
        if combo:
            h.score += ((combo["win_rate"] * 15 + combo["fuku_rate"] * 8)
                        * params.get("db_jockey", 0.0))

        if feat.get("interval") is not None:
            h.score += params.get("db_interval", 0.0) * _interval_pts(feat["interval"])

        h.reasons.extend(_reasons(dist, combo, params))
