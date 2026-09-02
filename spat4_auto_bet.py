#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPAT4 自動投票支援(spat4_auto_bet.py)
======================================
spat4_bet.py が作った投票リスト(spat4bet/spat4_bets_YYYYMMDD.csv)をもとに、
ブラウザ(Playwright)でSPAT4の投票画面への入力を代行する「半自動」ツールです。

★安全のための設計方針★
1. ログインはご自身で行います。加入者番号・利用者ID・暗証番号・P-ARS番号
   などの認証情報は、このスクリプトでは一切保存・入力しません。自動で
   ブラウザを開いたあと、ログインが終わるまでこのスクリプトは一時停止する
   ので、その間にご自身の手でログインしてください。
2. 購入の最終確定は必ずご自身でクリックしてください。「購入」「確定」
   「決済」「注文」「実行」など最終操作を意味する語を含むボタンは、この
   スクリプトが自動でクリックすることはありません(コードの安全チェックで
   明示的にブロックしています / is_final_action)。自動化するのはレース・
   式別・馬番・金額といった入力項目の入力までです。
3. SPAT4サイトの画面構造は非公開かつ予告なく変わるため、要素の指定
   (セレクタ)は未検証の best-effort です。動かない場合は --capture で
   実際の画面のHTML/スクリーンショットを保存できるので、それをもとに
   このファイル内の fill_one_bet() を調整してください
   (fetch_jra.py の --debug と同じ考え方です)。
4. SPAT4の会員規約(第三者への投票委託の禁止 等)はご自身でご確認の上、
   ご自身の判断・自己責任でお使いください。馬券は20歳以上の方のみ、
   余裕資金の範囲でお楽しみください。

初回セットアップ:
    pip install playwright
    playwright install chromium

使い方:
    python spat4_auto_bet.py ./horselist/spat4bet/spat4_bets_20260812.csv
    python spat4_auto_bet.py ./horselist --date 20260812
    python spat4_auto_bet.py ./horselist --date 20260812 --debug     # 各ステップでスクショ/HTML保存
    python spat4_auto_bet.py --capture                                # 画面構造を調べるための単発キャプチャ
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

SPAT4_URL = "https://www.spat4.jp/keiba/pc"

# 最終確定を意味する語。この語を含むボタン/リンクは絶対に自動クリックしない。
FINAL_ACTION_WORDS = [
    "購入", "確定", "決済", "注文", "実行", "送信",
    "Submit", "Confirm", "Purchase", "Buy", "Pay",
]


def is_final_action(label: str) -> bool:
    if not label:
        return False
    return any(w in label for w in FINAL_ACTION_WORDS)


def safe_click(page, locator, label, progress):
    """最終確定に類する操作は絶対にクリックしない安全ラッパー。"""
    if is_final_action(label):
        progress(f"  ⛔ 「{label}」は最終確定に類する操作のため自動クリックしません。ご自身でクリックしてください。")
        return False
    locator.click()
    return True


# ============================================================
# 入力(spat4_bet.py が出力したCSV)
# ============================================================

def find_bet_csv(path_str, ymd):
    path = Path(path_str)
    if path.is_file():
        return path
    if path.is_dir():
        candidates = [
            path / "spat4bet" / f"spat4_bets_{ymd}.csv",
            path / f"spat4_bets_{ymd}.csv",
        ]
        for c in candidates:
            if c.exists():
                return c
        hits = sorted(path.glob(f"**/spat4_bets_{ymd}.csv"))
        if hits:
            return hits[0]
        raise RuntimeError(f"{path} に spat4_bets_{ymd}.csv が見つかりません。先に spat4_bet.py を実行してください。")
    raise RuntimeError(f"パスが見つかりません: {path}")


def load_bets(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ============================================================
# ブラウザ操作(Playwright)
# ============================================================

def _dump(page, debug_dir, name):
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  (デバッグ保存に失敗: {e})")


def wait_for_manual_login(page):
    print("=" * 56)
    print(" ブラウザでSPAT4にログインしてください。")
    print(" 加入者番号・利用者ID・暗証番号・P-ARS番号はご自身で入力します。")
    print(" (このスクリプトは認証情報を一切扱いません)")
    print("=" * 56)
    input("ログインが完了したら、このターミナルでEnterキーを押してください... ")


def fill_one_bet(page, bet, progress, debug_dir=None, idx=0):
    """1点分の投票内容(式別・レース・馬番・金額)を入力する。

    ★未検証★ SPAT4の実画面のHTML構造が分からない状態で書いた
    best-effort の実装です。ラベルテキストでの検索(get_by_label /
    get_by_role)を使うことで多少の構造変化には強くしていますが、
    実際に動かして調整することを前提にしています。うまく行かない
    項目があれば --capture で保存したHTMLを見ながら、この関数の
    該当行だけを直してください。
    """
    place, race_no = bet["競馬場"], bet["レース番号"]
    umaban, stake = bet["馬番"], bet["金額(円)"]

    # 券種(単勝)の選択。既に単勝が既定の画面や、券種選択が別画面の
    # サイトもあるため、見つからなくても致命的エラーにはしない。
    for name in ("単勝",):
        try:
            page.get_by_role("radio", name=name).check(timeout=2000)
            break
        except Exception:
            try:
                page.get_by_label(name).check(timeout=2000)
                break
            except Exception:
                continue

    # 競馬場の選択
    try:
        page.get_by_label("競馬場").select_option(label=place, timeout=3000)
    except Exception:
        try:
            page.get_by_text(place, exact=False).first.click(timeout=3000)
        except Exception as e:
            raise RuntimeError(f"競馬場「{place}」の選択に失敗: {e}")

    # レース番号の選択
    try:
        page.get_by_label("レース").select_option(label=f"{race_no}R", timeout=3000)
    except Exception:
        try:
            page.get_by_text(f"{race_no}R", exact=False).first.click(timeout=3000)
        except Exception as e:
            raise RuntimeError(f"レース「{race_no}R」の選択に失敗: {e}")

    # 馬番の入力
    try:
        page.get_by_label("馬番").fill(str(umaban), timeout=3000)
    except Exception as e:
        raise RuntimeError(f"馬番の入力に失敗: {e}")

    # 金額の入力(円→100円単位の「点数」表記のサイトもあるため要確認)
    try:
        page.get_by_label("金額").fill(str(stake), timeout=3000)
    except Exception as e:
        raise RuntimeError(f"金額の入力に失敗: {e}")

    if debug_dir:
        _dump(page, debug_dir, f"{idx:02d}_{place}_{race_no}R")

    # 「投票リストに追加」的な中間ボタンがあればクリックする。
    # ただし最終確定に類する語を含むボタンは safe_click 内で自動スキップされる。
    for name in ("追加", "セット", "カートに入れる"):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count() > 0:
                safe_click(page, btn.first, name, progress)
                break
        except Exception:
            continue


def run_auto_bet(csv_path, debug=False, headless=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ playwright が入っていません。次のコマンドでインストールしてください:")
        print("   pip install playwright")
        print("   playwright install chromium")
        sys.exit(1)

    bets = load_bets(csv_path)
    if not bets:
        print("投票対象がありません(spat4_bet.pyの出力に0件でした)。")
        return

    print(f"📄 {csv_path} から {len(bets)}件を読み込みました。")
    debug_dir = csv_path.parent / "spat4_debug"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(SPAT4_URL)

        wait_for_manual_login(page)

        ok, ng = 0, 0
        for i, b in enumerate(bets, 1):
            print(f"\n[{i}/{len(bets)}] {b['競馬場']} {b['レース番号']}R 単勝 "
                  f"{b['馬番']}番 {b['馬名']} {int(b['金額(円)']):,}円 を入力します...")
            try:
                fill_one_bet(page, b, print, debug_dir if debug else None, i)
                ok += 1
            except Exception as e:
                ng += 1
                print(f"  ⚠ 自動入力に失敗しました: {e}")
                print("  → このレースはご自身で手動入力してください。")
                _dump(page, debug_dir, f"{i:02d}_error")

        print("\n" + "=" * 56)
        print(f" 自動入力できた候補: {ok}件 / 手動対応が必要: {ng}件")
        print(" 必ず投票内容確認画面でレース・馬番・金額をご自身の目で確認し、")
        print(" 購入の最終確定は必ずご自身でクリックしてください。")
        print(" (このスクリプトが購入確定ボタンを押すことはありません)")
        print("=" * 56)
        input("\nブラウザは開いたままにします。確認が終わったらこのターミナルでEnterを押してください... ")
        browser.close()


def run_capture(debug_dir):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ playwright が入っていません。'pip install playwright' と 'playwright install chromium' を実行してください。")
        sys.exit(1)

    debug_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(SPAT4_URL)
        wait_for_manual_login(page)
        print("投票入力画面まで手動で移動してください(式別・馬番・金額の入力欄が見える状態)。")
        input("移動できたら、このターミナルでEnterを押してください(HTML/スクショを保存します)... ")
        _dump(page, debug_dir, "capture")
        print(f"📸 {debug_dir}/capture.png と capture.html を保存しました。")
        print("   このHTMLを見ながら fill_one_bet() のセレクタを調整してください。")
        input("終了する場合はEnterを押してください(ブラウザを閉じます)... ")
        browser.close()


def main():
    ap = argparse.ArgumentParser(
        description="spat4_bet.py の投票リストをもとにSPAT4の入力を半自動化(購入確定は必ず手動)")
    ap.add_argument("horselist", nargs="?", default=None,
                    help="spat4_bets_YYYYMMDD.csv へのパス、またはそれを含むフォルダ")
    ap.add_argument("--date", type=str, default=None, help="対象日 YYYYMMDD")
    ap.add_argument("--debug", action="store_true", help="各ステップのスクショ/HTMLを spat4_debug/ に保存")
    ap.add_argument("--headless", action="store_true",
                    help="ヘッドレスで実行(非推奨: ログイン・最終確認ができなくなります)")
    ap.add_argument("--capture", action="store_true",
                    help="投票せず、画面のHTML/スクショを1回だけ保存する調査モード")
    args = ap.parse_args()

    if args.capture:
        run_capture(Path(args.horselist or ".") / "spat4_debug")
        return

    if not args.horselist:
        print("❌ horselist(CSVまたはフォルダ)を指定してください。")
        sys.exit(1)

    ymd = args.date or date.today().strftime("%Y%m%d")
    try:
        csv_path = find_bet_csv(args.horselist, ymd)
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    print("=" * 56)
    print(" ⚠ 実際の資金が動く操作です。次の点を必ずご確認ください:")
    print("  ・投票内容(レース/馬番/金額)は購入前にご自身の目で確認する")
    print("  ・購入の最終確定は必ずご自身でクリックする(自動では行いません)")
    print("  ・SPAT4の会員規約はご自身でご確認の上、自己責任でお使いください")
    print("  ・馬券は20歳以上の方のみ、余裕資金の範囲でお楽しみください")
    print("=" * 56)
    if input("よろしければ y を入力してください: ").strip().lower() != "y":
        print("中止しました。")
        return

    if args.headless:
        print("⚠ --headless 指定ですが、ログインと最終確認のため画面表示モードで実行します。")

    run_auto_bet(csv_path, debug=args.debug, headless=False)


if __name__ == "__main__":
    main()
