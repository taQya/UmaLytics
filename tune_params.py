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
import re
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
    "kinryo":     (0.0, 100.0),
    "softmax_t":  (4.0, 25.0),
    "jra_affili":  (0.0, 20.0),
    # 出馬表ページ由来(deba_table.py)。キャッシュが無い日は効かないので0付近に落ちる
    "deba_recent":   (0.0, 25.0),
    "deba_margin":   (0.0, 8.0),
    "deba_agari":    (0.0, 8.0),
    "deba_pace":     (0.0, 8.0),
    "deba_interval": (0.0, 5.0),
    "deba_jockey":   (0.0, 6.0),
    # 前走と今回のクラス差による近走点補正の強度(0=無効〜1=設計値どおり)
    "deba_class_adj": (0.0, 1.5),
    # horses.sqlite3(生涯レース履歴)由来。DBが無い/紐付かない馬は効かないので
    # その場合は0付近に落ちる
    "db_recent":     (0.0, 20.0),
    "db_dist":       (0.0, 3.0),
    "db_jockey":     (0.0, 3.0),
    "db_interval":   (0.0, 5.0),
    "db_class_adj":  (0.0, 1.5),
}


def file_date(path: Path):
    """ファイル名から日付(YYYYMMDD文字列)を抽出する。
    '20260707_horselist.csv' 'JRA_2026_0717_horselist.csv' の両方に対応"""
    m = re.search(r"(20\d{2})_?(\d{2})_?(\d{2})", path.stem)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else path.stem


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
        for pk in eng.ev_bets(ranked, 100.0, payout, jra):
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
    base = evaluate(dataset, defaults)  # この時点で h.db(生涯レース履歴)もキャッシュされる
    n_db = sum(1 for rh, _, _ in dataset for h in rh if (h.db or {}).get("races"))
    progress(f"📋 horses.sqlite3の紐付け: {n_db}/{n_horses}頭 "
             f"({n_db / n_horses:.0%})")
    if not n_db:
        progress("   ※ horse_db.py --fetch で馬データを蓄積すると近走・距離適性・"
                 "騎手コンビ等の指標が使えます")

    # 出馬表/DB由来の重みを0にした成績。この差が「収集したデータの効き」になる
    off = dict(defaults, **{k: 0.0 for k in defaults
                             if k.startswith("deba_") or k.startswith("db_")})
    base_off = evaluate(dataset, off)
    progress(f"\n📊 出馬表・DB不使用: ◎的中 {base_off[0]}/{base_off[3]} "
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
    progress(f"   出馬表・DB不使用との差: ◎的中 {best_r[0] - base_off[0]:+d} "
             f"/ 平均順位 {best_r[1] - base_off[1]:+.2f} "
             f"/ 回収率 {best_r[2] - base_off[2]:+.0f}%")
    return best_p, base, best_r, n_races, base_off


def tune_with_holdout(files, iters=300, objective="hits", holdout_frac=0.25,
                       seed=42, progress=print):
    """日付でtrain/holdoutに分割し、trainだけでチューニングしてholdout
    (学習に一切使っていない=未来を模した期間)で成績を検証する。
    tune_params.py単体の「学習データ上の成績」だけでは過学習に気づけない
    ため、実運用に近い形で「本当に改善しているか」を確認するための機能。
    戻り値の最後(hold_after)がNoneの場合は分割できなかったことを示す。"""
    paths = sorted((Path(f) for f in files), key=lambda p: (file_date(p), p.name))
    if len(paths) < 8:
        progress(f"⚠ ファイル数({len(paths)})が少なくholdout分割の信頼性が低いため、"
                 "分割せず全データで学習します。")
        best_p, base, best_r, n_races, base_off = tune(files, iters, objective, seed, progress)
        return best_p, base, best_r, n_races, base_off, None

    n_holdout = max(1, round(len(paths) * holdout_frac))
    train_paths, holdout_paths = paths[:-n_holdout], paths[-n_holdout:]
    progress(f"📅 学習期間: {file_date(train_paths[0])}〜{file_date(train_paths[-1])}"
             f"({len(train_paths)}日) / 検証期間(未来役・学習に未使用): "
             f"{file_date(holdout_paths[0])}〜{file_date(holdout_paths[-1])}"
             f"({len(holdout_paths)}日)\n")

    defaults_before = dict(eng.PARAMS)
    best_p, base, best_r, n_races, base_off = tune(
        [str(p) for p in train_paths], iters, objective, seed, progress)

    hold_dataset = []
    for p in holdout_paths:
        d = load_day(p)
        if d:
            hold_dataset.extend(d)
    if not hold_dataset:
        progress("⚠ 検証期間にpayback付きデータがありませんでした")
        return best_p, base, best_r, n_races, base_off, None

    hold_before = evaluate(hold_dataset, defaults_before)
    hold_after = evaluate(hold_dataset, best_p)
    progress(f"\n🔍 検証期間({len(holdout_paths)}日・学習に未使用)での成績"
             "(これが実運用に一番近い数字):")
    progress(f"   調整前パラメータ: ◎的中 {hold_before[0]}/{hold_before[3]} "
             f"/ 平均予想順位 {hold_before[1]:.2f}位 / EV回収率 {hold_before[2]:.0f}%")
    progress(f"   調整後パラメータ: ◎的中 {hold_after[0]}/{hold_after[3]} "
             f"/ 平均予想順位 {hold_after[1]:.2f}位 / EV回収率 {hold_after[2]:.0f}%")
    if score_key(hold_after, objective) <= score_key(hold_before, objective):
        progress("⚠ 検証期間では改善していません。学習データへの過学習の可能性が高いので、"
                 "この調整結果の採用は見送るか、iters/データ量を見直してください。")
    else:
        progress("✅ 学習に使っていない期間でも改善しています(過学習ではなさそう)。")

    eng.PARAMS.update(defaults_before)
    return best_p, base, best_r, n_races, base_off, hold_after


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
    ap.add_argument("--holdout", type=float, default=0.25,
                    help="検証用に末尾何割の日付を学習から除外するか(デフォルト0.25。"
                         "0にすると検証なしで全データ学習=従来動作)")
    args = ap.parse_args()
    try:
        if args.holdout > 0:
            _, _, _, _, _, hold_after = tune_with_holdout(
                args.horselists, args.iters, args.objective, args.holdout)
            if hold_after is not None:
                print("\n" + "=" * 60)
                print("📦 検証が終わったので、最終的な保存用パラメータは全データ"
                      "(学習期間+検証期間)で学習し直します。")
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
