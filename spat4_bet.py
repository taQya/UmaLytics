#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPAT4 投票リスト生成(spat4_bet.py)
======================================
予想エンジンの「期待値買い」で、期待値がしきい値を超えた単勝を抽出し、
資金管理を適用した「投票リスト」を出力します。

★重要★ このツールは投票リストを作るところまでを担当します。
SPAT4サイトへのログイン・購入の確定は必ずご自身で行ってください。
- 加入者番号・利用者ID・暗証番号・P-ARS番号などの認証情報は、この
  ツールでは一切扱いません(自動入力もしません)。金融口座の認証を
  プログラムに任せるのは、規約違反・不正利用・アカウント凍結の
  リスクがあるためです。
- ネット投票サービスは自動化ツールによるアクセスを規約で制限して
  いる場合があります。生成したリストは「手入力の下書き」として使い、
  最終的な内容確認と投票確定は人の目と手で行ってください。
- 馬券は20歳以上・自己責任で。1日の上限予算を必ず設定し、余裕資金の
  範囲で楽しんでください。

使い方:
    python spat4_bet.py 20260704_horselist.csv --ev 130 --unit 100 --budget 3000
    python spat4_bet.py jra_20260705_horselist.csv --stake kelly --bankroll 50000
    python spat4_bet.py ./houselist --date 20260705         # フォルダ+日付

出力(入力と同じフォルダの spat4bet/ サブディレクトリ):
    spat4bet/spat4_bets_YYYYMMDD.txt  … 人が読む投票指示(競馬場/R/馬番/金額)
    spat4bet/spat4_bets_YYYYMMDD.csv  … 表計算・マークカード転記用の構造化データ

しきい値・単位・予算は、実オッズが取得できているデータ(fetch_jraで
取得したCSV等)ほど期待値の精度が上がります。人気からの推定オッズだけの
場合は、実オッズと乖離することがある点にご注意ください。
"""

import argparse
import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path

import keiba_yosou as eng


# 古いバージョンの keiba_yosou.py には predict_race が無い場合があるため、
# 無ければここで補う(score_horse + adjust_kinryo を再現)。
if not hasattr(eng, "predict_race"):
    def _predict_race(horses, info=None):
        for h in horses:
            try:
                eng.score_horse(h, info)
            except TypeError:
                eng.score_horse(h)  # infoを取らない更に古い版
        eng.adjust_kinryo(horses)
        return sorted(horses, key=lambda x: x.score, reverse=True)
    eng.predict_race = _predict_race


# ============================================================
# 入力解決
# ============================================================

def resolve_horselists(path_str, ymd):
    path_str = str(path_str).replace("{today}", ymd).replace("{date}", ymd)
    path = Path(path_str)
    if path.is_dir():
        y, mo, d = ymd[:4], ymd[4:6], ymd[6:8]
        hits = sorted(set(
            list(path.glob(f"*{ymd}*horselist*.csv"))
            + list(path.glob(f"*{y}_{mo}{d}*horselist*.csv"))))
        if not hits:
            raise RuntimeError(f"{path} に {ymd} のhorselist CSVが見つかりません")
        return hits
    if not path.exists():
        raise RuntimeError(f"CSVが見つかりません: {path}")
    return [path]


def detect_ymd(path, fallback):
    return eng.detect_ymd_from_name(path, fallback)


# ============================================================
# 投票リスト生成
# ============================================================

def collect_bets(horses, race_info, ev_threshold):
    """しきい値超えの単勝候補を集める
    戻り値: [{place,race_no,umaban,name,prob,odds,ev,real,発走時刻}, ...]"""
    races = {}
    for h in horses:
        if not h.scratched:
            races.setdefault((h.place, h.race_no), []).append(h)
    bets = []
    for key in sorted(races, key=lambda k: (k[0], k[1])):
        info = race_info.get(key, {})
        jra = eng.is_jra(key[0])
        ranked = eng.predict_race(races[key], info)
        payout = eng.TAKEOUT_RETURN_JRA if jra else eng.TAKEOUT_RETURN
        for pk in eng.ev_bets(ranked, ev_threshold, payout):
            h = pk["horse"]
            bets.append({
                "place": key[0], "race_no": key[1],
                "umaban": h.umaban, "name": h.name,
                "prob": pk["prob"], "odds": pk["odds"],
                "ev": pk["ev"], "real": pk.get("real", False),
                "hasso": info.get("発走時刻", ""),
                "jra": jra,
            })
    return bets


def apply_staking(bets, mode, unit, budget, bankroll, kelly_frac, min_unit=100):
    """各投票に金額を割り当てる。予算内に収まるよう調整。
    mode: 'flat'(定額) / 'kelly'(ケリー基準の比例配分)
    金額は min_unit(通常100円)単位に丸める。"""
    if not bets:
        return bets, 0

    if mode == "kelly":
        # ケリー: f = (p*b - (1-p)) / b, b=odds-1。負や過大はクリップ
        weights = []
        for b in bets:
            odds, p = b["odds"], b["prob"]
            bnet = max(0.01, odds - 1)
            f = (p * bnet - (1 - p)) / bnet
            f = max(0.0, f) * kelly_frac  # フラクショナルケリー
            b["_kelly_f"] = f
            weights.append(f)
        total_f = sum(weights)
        for b, w in zip(bets, weights):
            raw = bankroll * w if total_f > 0 else 0
            b["stake"] = int(round(raw / min_unit)) * min_unit
    else:  # flat
        for b in bets:
            b["stake"] = unit

    # 予算キャップ: 期待値の高い順に予算内で採用
    bets_sorted = sorted(bets, key=lambda x: -x["ev"])
    spent = 0
    for b in bets_sorted:
        if b["stake"] < min_unit:
            b["stake"] = 0
            b["skipped"] = "金額が最低単位未満"
            continue
        if budget and spent + b["stake"] > budget:
            # 予算に収まるだけ買う(最低単位まで)
            room = budget - spent
            if room >= min_unit:
                b["stake"] = (room // min_unit) * min_unit
            else:
                b["stake"] = 0
                b["skipped"] = "予算上限に到達"
        spent += b["stake"]
    total = sum(b["stake"] for b in bets)
    return bets, total


PLACE_ALIAS = {  # 表記ゆれの正規化(表示用)
    "帯広ば": "帯広(ば)", "ばんえい": "帯広(ば)",
}


def write_outputs(bets, total, out_dir, ymd, params):
    # 出力は out_dir 直下ではなく spat4bet/ サブディレクトリにまとめる
    out_dir = Path(out_dir) / "spat4bet"
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"spat4_bets_{ymd}.txt"
    csvp = out_dir / f"spat4_bets_{ymd}.csv"

    active = [b for b in bets if b.get("stake", 0) > 0]

    # --- 人が読む投票指示 ---
    lines = []
    lines.append("=" * 56)
    lines.append(f" SPAT4 投票リスト  {ymd[:4]}/{ymd[4:6]}/{ymd[6:]}")
    lines.append("=" * 56)
    lines.append(f" 券種: 単勝 / 期待値しきい値: {params['ev']:.0f}% 以上")
    stake_desc = (f"定額 {params['unit']}円/点" if params["mode"] == "flat"
                  else f"ケリー×{params['kelly_frac']:.2f}(bankroll {params['bankroll']:,}円)")
    lines.append(f" 資金配分: {stake_desc}")
    if params["budget"]:
        lines.append(f" 1日上限予算: {params['budget']:,}円")
    lines.append("-" * 56)
    if not active:
        lines.append(" 本日、しきい値を超える投票はありません(見送り)。")
    else:
        cur_place = None
        for b in active:
            pl = PLACE_ALIAS.get(b["place"], b["place"])
            if pl != cur_place:
                cur_place = pl
                lines.append(f"\n【{pl}】")
            hasso = f" 発走{b['hasso']}" if b["hasso"] else ""
            odds_l = "実" if b["real"] else "推定"
            lines.append(
                f"  {b['race_no']:>2}R  単勝 {b['umaban']:>2}番 {b['name']:<12} "
                f"{b['stake']:>6,}円   "
                f"[勝率{b['prob']:.0%}×{odds_l}{b['odds']:.1f}倍=EV{b['ev']:.0f}%]")
    lines.append("\n" + "-" * 56)
    lines.append(f" 投票点数: {len(active)}点 / 合計投資額: {total:,}円")
    if params["budget"]:
        lines.append(f" 予算残り: {params['budget'] - total:,}円")
    lines.append("=" * 56)
    if active and any(not b["real"] for b in active):
        lines.append(" ⚠ このリストは【推定オッズ】で期待値を算出しています。")
        lines.append("   人気からの推定のため実オッズと大きくズレることがあり、")
        lines.append("   期待値(EV%)が過大に出やすい点にご注意ください。")
        lines.append("   投票前にSPAT4で実オッズを必ず確認してください。")
    lines.append(" ※このリストは投票の下書きです。SPAT4サイトで内容を確認し、")
    lines.append("   ご自身でログイン・投票の確定を行ってください。")
    lines.append("   認証情報の自動入力・自動購入は行いません。")
    lines.append("   馬券は20歳以上・自己責任で、余裕資金の範囲でお楽しみください。")
    txt.write_text("\n".join(lines), encoding="utf-8")

    # --- 構造化CSV(マークカード転記/表計算用) ---
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["競馬場", "レース番号", "券種", "馬番", "馬名",
                    "金額(円)", "発走時刻", "推定勝率", "オッズ",
                    "オッズ種別", "期待値pct"])
        for b in active:
            w.writerow([b["place"], b["race_no"], "単勝", b["umaban"], b["name"],
                        b["stake"], b["hasso"], f"{b['prob']:.3f}",
                        f"{b['odds']:.1f}", "実" if b["real"] else "推定",
                        f"{b['ev']:.0f}"])
    return txt, csvp, len(active)


def run(horselist_path, ev_threshold, mode, unit, budget, bankroll,
        kelly_frac, out_dir, ymd, progress=print):
    horses = eng.load_horselist(horselist_path)
    rl = eng.find_sibling(horselist_path, "racelist")
    race_info = eng.load_racelist(rl) if rl else {}

    bets = collect_bets(horses, race_info, ev_threshold)
    real_n = sum(1 for b in bets if b["real"])
    if bets and real_n == 0:
        progress("⚠ このデータには実オッズが無いため、推定オッズで期待値を計算します。")
        progress("   実際の投票前にSPAT4で実オッズを必ず確認してください。")

    bets, total = apply_staking(bets, mode, unit, budget, bankroll,
                                kelly_frac)
    params = {"ev": ev_threshold, "mode": mode, "unit": unit,
              "budget": budget, "bankroll": bankroll, "kelly_frac": kelly_frac}
    txt, csvp, n = write_outputs(bets, total, out_dir, ymd, params)
    progress(f"💴 投票{n}点 / 合計{total:,}円")
    progress(f"📝 {txt.name} と {csvp.name} を出力しました")
    return txt, csvp, n, total


def main():
    ap = argparse.ArgumentParser(
        description="期待値しきい値超えの単勝でSPAT4投票リストを生成(投票確定は手動)")
    ap.add_argument("horselist", help="horselist CSV / フォルダ / {today}プレースホルダ")
    ap.add_argument("--date", type=str, default=None, help="対象日 YYYYMMDD")
    ap.add_argument("--ev", type=float, default=130.0,
                    help="期待値しきい値%%(デフォルト130)")
    ap.add_argument("--stake", choices=["flat", "kelly"], default="flat",
                    help="資金配分: flat=定額 / kelly=ケリー基準")
    ap.add_argument("--unit", type=int, default=100,
                    help="flat時の1点あたり金額(デフォルト100円)")
    ap.add_argument("--budget", type=int, default=0,
                    help="1日の上限予算(円)。0で無制限だが設定を強く推奨")
    ap.add_argument("--bankroll", type=int, default=30000,
                    help="kelly時の総資金(円)")
    ap.add_argument("--kelly-frac", type=float, default=0.25,
                    help="フラクショナルケリー係数(デフォルト0.25=1/4ケリー)")
    args = ap.parse_args()

    ymd_default = args.date or date.today().strftime("%Y%m%d")
    try:
        paths = resolve_horselists(args.horselist, ymd_default)
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if not args.budget and args.stake == "flat":
        print("⚠ --budget(1日上限予算)が未設定です。使いすぎ防止のため設定を推奨します。")

    for hp in paths:
        ymd = args.date or detect_ymd(hp, ymd_default)
        print(f"\n📂 {hp.name} ({'中央' if any(eng.is_jra(h.place) for h in eng.load_horselist(hp)) else '地方'})")
        run(hp, args.ev, args.stake, args.unit, args.budget,
            args.bankroll, args.kelly_frac, hp.parent, ymd)

    print("\n" + "=" * 56)
    print("次のステップ: 出力された spat4_bets_*.txt を確認し、")
    print("SPAT4サイト(https://www.spat4.jp/)にご自身でログインして")
    print("内容を見比べながら投票してください。自動購入は行いません。")
    print("馬券は20歳以上・自己責任で、余裕資金の範囲でお楽しみください。")


if __name__ == "__main__":
    main()
