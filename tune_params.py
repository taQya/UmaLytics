#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
予想パラメータ自動チューニング(tune_params.py)
==================================================
horselist + payback(結果)のペアを使って、予想エンジンの重み
パラメータをランダムサーチで調整し、keiba_params.json に保存します。
保存されたパラメータは keiba_yosou.py / keiba_yosou_gui.py が
起動時に自動で読み込みます。

使い方:
    python tune_params.py jra_20260705_horselist.csv
    python tune_params.py day1_horselist.csv day2_horselist.csv ...  # 複数日
    python tune_params.py *_horselist.csv --iters 500
    python tune_params.py ... --objective recovery   # 回収率を最大化

【過学習に関する重要な注意】
1日分(数十レース)だけで調整すると「その日に合わせすぎた」パラメータに
なりがちで、翌週の的中率はむしろ下がることがあります。
- できるだけ複数週のデータを貯めて(horselist+paybackのペアを保存)、
  まとめて渡してください
- 表示される成績は学習に使ったデータ上(イン・サンプル)の数字です。
  未来の成績を保証しません
"""

import argparse
import json
import random
import sys
from pathlib import Path

import keiba_yosou as eng  # CLI版エンジンを共用

# 探索範囲(デフォルト値の周辺)
SEARCH_SPACE = {
    "total_win":  (10.0, 80.0),
    "total_fuku": (0.0, 50.0),
    "recent":     (0.0, 2.0),
    "apt":        (0.0, 2.5),
    "jockey":     (0.0, 3.0),
    "ninki":      (0.0, 1.5),
    "kinryo":     (0.0, 500.0),
    "softmax_t":  (4.0, 25.0),
    "jra_affili":  (0.0, 20.0),
    # 出馬表ページ由来(deba_table.py)。キャッシュが無い日は効かないので0付近に落ちる
    "deba_recent":   (0.0, 25.0),
    "deba_margin":   (0.0, 8.0),
    "deba_agari":    (0.0, 8.0),
    "deba_pace":     (0.0, 8.0),
    "deba_interval": (0.0, 5.0),
    "deba_jockey":   (0.0, 6.0),
}


def load_day(horselist_path: Path):
    """1日分のデータを読み込む。結果(payback)が無ければNone"""
    horses = eng.load_horselist(horselist_path)
    rl = eng.find_sibling(horselist_path, "racelist")
    pb = eng.find_sibling(horselist_path, "payback")
    if not pb:
        return None
    race_info = eng.load_racelist(rl) if rl else {}
    paybacks = eng.load_payback(pb)
    races = {}
    for h in horses:
        if not h.scratched:
            races.setdefault((h.place, h.race_no), []).append(h)
    # 結果があるレースのみ
    data = []
    for key, rh in races.items():
        if key in paybacks:
            data.append((rh, race_info.get(key), paybacks[key]))
    return data


def evaluate(dataset, params):
    """パラメータでの成績を返す: (◎的中数, 勝ち馬平均予想順位, EV回収率, 対象R数)"""
    eng.PARAMS.update(params)
    hits = 0
    rank_sum = 0.0
    ev_invest = ev_ret = 0
    n = 0
    for rh, info, pb in dataset:
        n += 1
        ranked = eng.predict_race(list(rh), info)
        win = pb["勝ち馬番"]
        if ranked[0].umaban == win:
            hits += 1
        rank = next((i + 1 for i, h in enumerate(ranked) if h.umaban == win),
                    len(ranked))
        rank_sum += rank
        jra = eng.is_jra(rh[0].place)
        payout = eng.TAKEOUT_RETURN_JRA if jra else eng.TAKEOUT_RETURN
        for pk in eng.ev_bets(ranked, 100.0, payout):
            ev_invest += 100
            if pk["horse"].umaban == win:
                ev_ret += pb["単勝払戻"]
    avg_rank = rank_sum / n if n else 99
    recovery = ev_ret / ev_invest * 100 if ev_invest else 0
    return hits, avg_rank, recovery, n


def score_key(result, objective):
    hits, avg_rank, recovery, _ = result
    if objective == "recovery":
        return (recovery, hits, -avg_rank)
    return (hits, -avg_rank, recovery)  # 的中数 → 勝ち馬の予想順位 → 回収率


def tune(files, iters=300, objective="hits", seed=42, progress=print):
    dataset = []
    for f in files:
        d = load_day(Path(f))
        if d is None:
            progress(f"⚠ {Path(f).name}: paybackが見つからないためスキップ")
            continue
        dataset.extend(d)
        progress(f"📂 {Path(f).name}: 結果あり{len(d)}レースを学習データに追加")
    if not dataset:
        raise RuntimeError("結果(payback)付きのデータがありません")
    n_races = len(dataset)
    progress(f"\n🎯 学習データ: 合計{n_races}レース / 目的: "
             f"{'回収率' if objective == 'recovery' else '◎的中数'}の最大化")
    if n_races < 30:
        progress("⚠ レース数が少なめです。1日分だけの調整は過学習しやすいので、")
        progress("   複数週のデータを貯めてから再調整するのがおすすめです。")
    if n_races < 1500:
        progress(f"⚠ 探索次元が{len(SEARCH_SPACE)}個あるため、レース数が少ないうちは"
                 "--iters を増やす(1000以上推奨)か、複数週分のデータを貯めてから"
                 "の調整をおすすめします。")

    n_horses = sum(len(rh) for rh, _, _ in dataset)
    n_deba = sum(1 for rh, _, _ in dataset for h in rh if h.deba)
    progress(f"📋 出馬表データの紐付け: {n_deba}/{n_horses}頭 "
             f"({n_deba / n_horses:.0%})")
    if not n_deba:
        progress("   ※ deba_table.py で取得すると近走・脚質の指標が使えます")

    defaults = dict(eng.PARAMS)
    base = evaluate(dataset, defaults)
    # 出馬表由来の重みを0にした成績。この差が「収集したデータの効き」になる
    off = dict(defaults, **{k: 0.0 for k in defaults if k.startswith("deba_")})
    base_off = evaluate(dataset, off)
    progress(f"\n📊 出馬表データ不使用: ◎的中 {base_off[0]}/{base_off[3]} "
             f"/ 勝ち馬の平均予想順位 {base_off[1]:.2f}位 / EV回収率 {base_off[2]:.0f}%")
    progress(f"📊 調整前(初期値): ◎的中 {base[0]}/{base[3]} "
             f"/ 勝ち馬の平均予想順位 {base[1]:.2f}位 / EV回収率 {base[2]:.0f}%")

    rng = random.Random(seed)
    best_p, best_r = dict(defaults), base
    for it in range(iters):
        if it < iters // 2 or best_p is None:
            cand = {k: rng.uniform(*v) for k, v in SEARCH_SPACE.items()}
        else:  # 後半はベスト近傍を探索
            cand = {}
            for k, (lo, hi) in SEARCH_SPACE.items():
                span = (hi - lo) * 0.15
                cand[k] = min(hi, max(lo, best_p[k] + rng.uniform(-span, span)))
        r = evaluate(dataset, cand)
        if score_key(r, objective) > score_key(best_r, objective):
            best_p, best_r = dict(cand), r
            progress(f"  ↑ 改善({it + 1}回目): ◎的中{r[0]} 平均順位{r[1]:.2f} 回収率{r[2]:.0f}%")

    eng.PARAMS.update(defaults)  # エンジンを元に戻す
    progress(f"\n📊 調整後(学習データ上): ◎的中 {best_r[0]}/{best_r[3]} "
             f"/ 勝ち馬の平均予想順位 {best_r[1]:.2f}位 / EV回収率 {best_r[2]:.0f}%")
    progress(f"   出馬表データ不使用との差: ◎的中 {best_r[0] - base_off[0]:+d} "
             f"/ 平均順位 {best_r[1] - base_off[1]:+.2f} "
             f"/ 回収率 {best_r[2] - base_off[2]:+.0f}%")
    return best_p, base, best_r, n_races, base_off


def save_params(params, meta, path=None):
    path = Path(path) if path else eng.app_dir() / "keiba_params.json"
    data = {k: round(v, 3) for k, v in params.items()}
    data["_meta"] = meta
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="結果データから予想パラメータを自動調整")
    ap.add_argument("horselists", nargs="+", help="horselist CSV(複数可。paybackは自動検出)")
    ap.add_argument("--iters", type=int, default=300, help="探索回数(デフォルト300)")
    ap.add_argument("--objective", choices=["hits", "recovery"], default="hits",
                    help="hits=◎的中数 / recovery=期待値買いの回収率")
    ap.add_argument("--out", type=Path, default=None, help="保存先(デフォルト: keiba_params.json)")
    args = ap.parse_args()
    try:
        best_p, base, best_r, n, base_off = tune(
            args.horselists, args.iters, args.objective)
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    meta = {"学習レース数": n, "調整前_的中": base[0], "調整後_的中": best_r[0],
            "調整前_回収率": round(base[2]), "調整後_回収率": round(best_r[2]),
            "出馬表なし_的中": base_off[0], "出馬表なし_回収率": round(base_off[2]),
            "注意": "学習データ上の成績です。未来の的中を保証しません"}
    path = save_params(best_p, meta, args.out)
    print(f"\n💾 保存しました: {path}")
    print("   keiba_yosou.py / keiba_yosou_gui.py は次回起動時から自動で使用します。")
    print("   元に戻すには keiba_params.json を削除してください。")
    print("⚠ 学習データ上の改善です。翌週も良くなる保証はありません(過学習に注意)。")


if __name__ == "__main__":
    main()
