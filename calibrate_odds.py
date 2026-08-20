#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市場シェア曲線の校正(calibrate_odds.py)
=====================================================================
期待値買い(ev_bets)は「人気順位→推定オッズ」に固定パラメータ ZIPF_ALPHA=1.35
の単純なべき乗則(Zipf)を使っていた。しかし実際の払戻データで検証すると、
2〜6番人気のオッズを20〜35%も過大評価しており、逆に9番人気以降は過小評価
していた(本命は実際よりシェアが低く出て、大穴は実際よりシェアが高く出る
「favorite-longshot bias」を単純なべき乗則では表現できないため)。

このスクリプトは horselist+payback のペアから「勝ち馬の人気順位と実際の
オッズ(単勝払戻÷100)」を集め、人気順位ごとの実際の市場シェア(=払戻率÷
オッズ)を集計する。的中した馬の実オッズはレース結果に関わらず出走前に
決まっていたものなので、この標本に偏りは生じない。

サンプルが少ない人気順位(目安16位以降)は、十分にサンプルがある範囲の
シェア比率(等比的な減衰)を延長して補う。

結果は odds_calibration.json に保存し、estimate_odds_map() が読み込んで
使う(地方競馬のみ。中央競馬は的中データが無いため従来のZipf式のまま)。

使い方:
    python calibrate_odds.py horselist/*_horselist.csv
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

import tune_params as tp

MIN_SAMPLES = 15   # この人気順位以上のサンプルがあれば実測値を信頼する
MAX_RANK = 20      # 出力する人気順位の上限(それ以降は最終順位の値を使い回す)
OUT_PATH = Path(__file__).resolve().parent / "odds_calibration.json"


def collect_winner_odds(dataset):
    """{人気順位: [実オッズ, ...]} を集める(勝ち馬のみ・中央競馬は除外)"""
    by_rank = {}
    for rh, info, pb in dataset:
        if not rh or tp.eng.is_jra(rh[0].place):
            continue
        win_h = next((h for h in rh if h.umaban == pb["勝ち馬番"]), None)
        if not win_h or win_h.ninki <= 0:
            continue
        by_rank.setdefault(win_h.ninki, []).append(pb["単勝払戻"] / 100.0)
    return by_rank


def build_share_table(by_rank, payout=tp.eng.TAKEOUT_RETURN,
                       min_samples=MIN_SAMPLES, max_rank=MAX_RANK):
    """実測オッズ→市場シェア(payout/odds)のテーブルを作り、
    サンプル不足の順位は既知範囲の減衰比を延長して埋める"""
    shares = {}
    for r, odds_list in sorted(by_rank.items()):
        if len(odds_list) >= min_samples:
            shares[r] = payout / statistics.median(odds_list)

    if not shares:
        raise RuntimeError("十分なサンプルがある人気順位がありません")

    known_ranks = sorted(shares)
    last_known = known_ranks[-1]
    # 終盤側の減衰比(share[r]/share[r-1])の平均で延長する
    ratios = [shares[r] / shares[r - 1] for r in known_ranks
              if r - 1 in shares and shares[r - 1] > 0]
    decay = statistics.mean(ratios) if ratios else 0.7
    decay = min(0.95, max(0.3, decay))  # 極端な値を避ける

    for r in range(last_known + 1, max_rank + 1):
        shares[r] = shares[last_known] * (decay ** (r - last_known))

    return shares, decay


def compare_with_old(shares, payout=tp.eng.TAKEOUT_RETURN, field_size=10):
    """従来のZipf式(頭数10と仮定)との比較表を表示する"""
    ZIPF_ALPHA = 1.35
    raw = {r: r ** (-ZIPF_ALPHA) for r in range(1, field_size + 1)}
    s = sum(raw.values())
    print(f"{'人気':>4} | {'旧推定(Zipf)':>10} | {'新推定(校正済)':>10} | 差")
    for r in range(1, min(field_size, max(shares)) + 1):
        old_odds = payout / (raw[r] / s)
        new_odds = payout / shares[r] if shares.get(r) else float("nan")
        print(f"{r:4d} | {old_odds:10.2f} | {new_odds:10.2f} | "
              f"{(new_odds / old_odds - 1) * 100:+.0f}%")


def main():
    ap = argparse.ArgumentParser(description="実際の払戻データから市場シェア曲線を校正する")
    ap.add_argument("horselists", nargs="+", help="horselist CSV(複数可。paybackは自動検出)")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    dataset = []
    for f in args.horselists:
        d = tp.load_day(Path(f))
        if d:
            dataset.extend(d)
    if not dataset:
        print("❌ 結果(payback)付きのデータがありません", file=sys.stderr)
        sys.exit(1)

    by_rank = collect_winner_odds(dataset)
    n_samples = sum(len(v) for v in by_rank.values())
    print(f"📊 地方競馬の的中データ {n_samples}件({len(by_rank)}人気順位ぶん)から校正します")

    shares, decay = build_share_table(by_rank, min_samples=args.min_samples)
    n_real = sum(1 for r, odds in by_rank.items() if len(odds) >= args.min_samples)
    print(f"   実測ベース: 1〜{max(r for r in by_rank if len(by_rank[r]) >= args.min_samples)}"
          f"番人気({n_real}順位) / それ以降は減衰比{decay:.3f}で延長")
    print()
    compare_with_old(shares)

    data = {
        "shares": {str(r): round(v, 5) for r, v in shares.items()},
        "_meta": {
            "サンプル数": n_samples,
            "実測順位数": n_real,
            "減衰比": round(decay, 3),
            "注意": "地方競馬の的中データのみで校正。中央競馬には未適用",
        },
    }
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 保存しました: {args.out}")
    print("   keiba_yosou.py / keiba_yosou_gui.py は次回起動時から自動で使用します。")


if __name__ == "__main__":
    main()
