#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WordPress自動投稿(post_wordpress.py)
========================================
予想アプリの出力をWordPress記事として自動投稿します。
標準ライブラリのみで動作します。

準備(最初に1回):
    1. WordPress管理画面 → ユーザー → プロフィール → アプリケーションパスワード
       で新規パスワードを発行(通常のログインパスワードは使わない)
    2. wp_config.json を本ファイルと同じフォルダに作成:
       {
         "site_url": "https://あなたのサイト.com",
         "username": "WPユーザー名",
         "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx",
         "status": "publish",        ← "draft"にすると下書き投稿
         "category_id": 0            ← 任意。カテゴリID(0なら未指定)
       }
       ※このファイルにはパスワードが入ります。共有・アップロードしないこと。

使い方:
    python post_wordpress.py --fetch              # JRAから当日出馬表を取得して予想記事を投稿
    python post_wordpress.py --csv jra_20260705_horselist.csv   # 手元CSVから投稿
    python post_wordpress.py --fetch --results    # 結果も取得し答え合わせ付きで記事を更新
    python post_wordpress.py --csv ... --dry-run  # 投稿せずHTMLをファイル保存(確認用)
    python post_wordpress.py ... --draft          # 下書きとして投稿

毎日の自動実行(Windows):
    run_daily.bat を任意の時刻でタスクスケジューラに登録してください。
    例: schtasks /create /tn "KeibaYosouPost" /tr "C:\\keiba\\run_daily.bat" /sc daily /st 09:00

同じ日の記事はスラッグ(keiba-yosou-YYYYMMDD)で検索して上書き更新するため、
朝に予想・夜に--resultsで答え合わせ、と1日2回実行しても記事は1本に保たれます。
"""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import keiba_yosou as eng

UA = "KeibaYosouChat/1.0 (WordPress auto poster)"


# ============================================================
# CSVパスの解決
# ============================================================

def resolve_csv(path_str, ymd):
    """--csv の指定を解決してhorselistのリストを返す。
    - {today} / {date} プレースホルダは ymd(YYYYMMDD)に置換
    - フォルダ指定なら中から *<ymd>*horselist*.csv を全て選択
      (中央: jra_YYYYMMDD_… と 地方: YYYYMMDD_… の両形式に対応。
       両方あればそれぞれ別記事として投稿されます)
    """
    path_str = str(path_str).replace("{today}", ymd).replace("{date}", ymd)
    path = Path(path_str)
    if path.is_dir():
        y, mo, d = ymd[:4], ymd[4:6], ymd[6:8]
        # 地方(YYYYMMDD)と中央(JRA_YYYY_MMDD)の両形式で探索
        hits = sorted(set(
            list(path.glob(f"*{ymd}*horselist*.csv"))
            + list(path.glob(f"*{y}_{mo}{d}*horselist*.csv"))))
        if not hits:
            avail = sorted(x.name for x in path.glob("*horselist*.csv"))[-5:]
            raise RuntimeError(
                f"{path} に {ymd} のhorselist CSVが見つかりません。\n"
                f"フォルダ内の候補(新しい順): {avail or 'なし'}\n"
                f"別の日付を使う場合は --date YYYYMMDD を指定してください。")
        return hits
    if not path.exists():
        raise RuntimeError(f"CSVが見つかりません: {path}")
    return [path]


def detect_ymd_from_name(path):
    """ファイル名から YYYYMMDD を検出。
    地方(YYYYMMDD_)・中央(JRA_YYYY_MMDD_)の両命名規則に対応。"""
    return eng.detect_ymd_from_name(path)


# ============================================================
# 設定
# ============================================================

def load_config(path=None):
    p = Path(path) if path else Path(__file__).parent / "wp_config.json"
    if not p.exists():
        raise RuntimeError(
            f"設定ファイルが見つかりません: {p}\n"
            "post_wordpress.py 冒頭の説明に従って wp_config.json を作成してください。")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for k in ("site_url", "username", "app_password"):
        if not cfg.get(k):
            raise RuntimeError(f"wp_config.json に {k} がありません")
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    cfg.setdefault("status", "publish")
    cfg.setdefault("category_id", 0)
    return cfg


# ============================================================
# 記事HTML生成
# ============================================================

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- デザイントークン(競馬新聞モチーフ / WPテーマに依存しないインラインスタイル) ---
INK = "#26241F"        # 墨
PAPER = "#FBF8F2"      # 紙(生成り)
LINE = "#E5DFD2"       # 罫線
TURF = "#1F6B45"       # ターフ緑(レース番号ゼッケン)
REDPEN = "#C0392B"     # 赤ペン(◎本命)
AMBER = "#B8860B"      # 期待値
AMBER_BG = "#FDF6E3"
MUTE = "#8A857B"       # 注記・見送り

SERIF = "'Hiragino Mincho ProN','Yu Mincho','Noto Serif JP',serif"
SANS = "'Hiragino Kaku Gothic ProN','Yu Gothic','Meiryo',sans-serif"

CONF_COLOR = {"S": REDPEN, "A": "#B26A00", "B": TURF, "C": MUTE, "-": MUTE}


def _stat(label, value):
    return (f"<div style='text-align:center;min-width:6em'>"
            f"<div style='font-size:22px;font-weight:700;font-family:{SERIF}'>{value}</div>"
            f"<div style='font-size:11px;color:{MUTE};letter-spacing:.1em'>{esc(label)}</div></div>")


def race_card(key, info, ranked, conf, picks, pb):
    """1レース分のカードHTML"""
    place, race_no = key
    conf_text, conf_rank = conf
    top = ranked[0]
    out = []

    out.append(f"<div style='background:{PAPER};border:1px solid {LINE};"
               f"border-radius:10px;padding:16px 16px 12px;margin:20px 0;"
               f"font-family:{SANS};color:{INK}'>")

    # ヘッダー: ゼッケン風バッジ + レース名(明朝) + 信頼度
    name = esc(info.get("レース名", "")) if info else ""
    meta = []
    if info:
        if info.get("発走時刻"):
            meta.append(f"発走 {esc(info['発走時刻'])}")
        if info.get("芝ダート") and info.get("距離"):
            mawari = info.get("回り", "")
            meta.append(f"{esc(info['芝ダート'])}{esc(mawari) + '回り・' if mawari and mawari != '直' else ''}{info['距離']}m")
        if info.get("頭数"):
            meta.append(f"{info['頭数']}頭")
    out.append(
        "<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap'>"
        f"<div style='background:{TURF};color:#fff;border-radius:8px;"
        f"padding:6px 10px;text-align:center;line-height:1.15;min-width:3.2em'>"
        f"<div style='font-size:11px'>{esc(place)}</div>"
        f"<div style='font-size:20px;font-weight:700'>{race_no}R</div></div>"
        f"<div style='flex:1;min-width:12em'>"
        f"<div style='font-family:{SERIF};font-size:19px;font-weight:700'>{name or f'第{race_no}レース'}</div>"
        f"<div style='font-size:12px;color:{MUTE}'>{' ／ '.join(meta)}</div></div>"
        f"<div style='border:1.5px solid {CONF_COLOR[conf_rank]};color:{CONF_COLOR[conf_rank]};"
        f"border-radius:999px;padding:2px 12px;font-size:12px;font-weight:700;white-space:nowrap'>"
        f"信頼度{conf_rank}・{esc(conf_text)}</div></div>")

    # 本命行(赤ペン)
    reason = "、".join(esc(r) for r in top.reasons[:3]) or "総合力上位"
    out.append(
        f"<div style='margin:12px 0 4px;font-size:17px'>"
        f"<span style='color:{REDPEN};font-size:24px;font-weight:700'>◎</span> "
        f"<strong style='color:{REDPEN}'>{top.umaban}番 {esc(top.name)}</strong>"
        f"<span style='font-size:13px;color:{MUTE}'>"
        f"{'　' + str(top.ninki) + '番人気' if top.ninki else ''}"
        f"{'／' + esc(top.jockey) + '騎手' if top.jockey else ''}</span></div>"
        f"<div style='font-size:13px;color:{INK};margin-bottom:10px'>{reason}</div>")

    # 印テーブル
    th = (f"<th style='padding:6px 8px;font-size:11px;color:{MUTE};"
          f"font-weight:400;border-bottom:1.5px solid {INK};text-align:left'>")
    rows = []
    for i, h in enumerate(ranked[:5]):
        mark = eng.MARKS[i] if i < len(eng.MARKS) else ""
        is_top = (i == 0)
        tr_bg = "#FDEEEA" if is_top else ("#F4F0E7" if i % 2 else "transparent")
        mark_style = (f"color:{REDPEN};font-weight:700" if is_top else f"color:{INK}")
        td = f"<td style='padding:7px 8px;font-size:14px;border-bottom:1px solid {LINE}'>"
        ninki = f"{h.ninki}人気" if h.ninki > 0 else "−"
        rows.append(
            f"<tr style='background:{tr_bg}'>"
            f"<td style='padding:7px 8px;font-size:18px;border-bottom:1px solid {LINE};{mark_style}'>{mark}</td>"
            f"{td}{h.umaban}</td>"
            f"{td}<strong>{esc(h.name)}</strong></td>"
            f"{td}{esc(h.jockey)}</td>"
            f"{td}{ninki}</td>"
            f"{td}{h.score:.1f}</td></tr>")
    out.append(
        "<table style='border-collapse:collapse;width:100%;margin:4px 0 10px'>"
        f"<thead><tr>{th}印</th>{th}馬番</th>{th}馬名</th>{th}騎手</th>"
        f"{th}人気</th>{th}スコア</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>")

    # 買い目
    if len(ranked) >= 3:
        a, b, c = ranked[0].umaban, ranked[1].umaban, ranked[2].umaban
        out.append(f"<div style='font-size:14px'>🎯 <strong>参考買い目</strong>　"
                   f"単勝 {a} ／ 馬複 {a}-{b} ／ 三連複 {a}-{b}-{c}</div>")

    # 期待値買い
    if picks:
        lines = "".join(
            f"<div style='margin:2px 0'>単勝 <strong>{pk['horse'].umaban}番 "
            f"{esc(pk['horse'].name)}</strong>　"
            f"<span style='font-size:12.5px'>推定勝率{pk['prob']:.0%} × "
            f"{'実' if pk.get('real') else '推定'}オッズ{pk['odds']:.1f}倍 ＝ "
            f"<strong>期待値{pk['ev']:.0f}%</strong></span></div>"
            for pk in picks)
        out.append(
            f"<div style='background:{AMBER_BG};border:1px solid {AMBER};"
            f"border-radius:8px;padding:10px 12px;margin-top:10px;font-size:14px;color:#5C4A00'>"
            f"<div style='font-weight:700;color:{AMBER};margin-bottom:4px'>💰 期待値買い</div>"
            f"{lines}</div>")
    else:
        out.append(f"<div style='margin-top:10px;font-size:13px;color:{MUTE}'>"
                   f"💰 期待値買い: このレースは<strong>見送り</strong>(期待値が基準未満)</div>")

    # 結果
    if pb:
        win = pb["勝ち馬番"]
        win_h = next((h for h in ranked if h.umaban == win), None)
        hit = top.umaban == win
        fk = top.umaban in pb.get("複勝馬番", [])
        if hit:
            color, bg, mark = TURF, "#EAF5EE", f"✅ ◎的中!(単勝{pb['単勝払戻']}円)"
        elif fk:
            color, bg, mark = AMBER, AMBER_BG, "🔶 ◎は3着以内(複勝圏)"
        else:
            color, bg, mark = MUTE, "#F1EFEA", "❌ ◎は圏外"
        out.append(
            f"<div style='border-left:4px solid {color};background:{bg};"
            f"border-radius:0 8px 8px 0;padding:8px 12px;margin-top:10px;font-size:14px'>"
            f"<strong>結果</strong>　勝ち馬 {win}番 {esc(win_h.name) if win_h else ''}　→　"
            f"<strong style='color:{color}'>{mark}</strong></div>")

    out.append("</div>")
    return "".join(out), (top.umaban == pb["勝ち馬番"] if pb else None)


def build_article(horses, race_info, paybacks, ev_threshold=100.0, ymd_str=None):
    """予想データから (タイトル, 記事HTML, 日付タグ) を生成
    ymd_str: 開催日 YYYYMMDD。省略時は本日"""
    races = {}
    for h in horses:
        if not h.scratched:
            races.setdefault((h.place, h.race_no), []).append(h)
    keys = sorted(races, key=lambda k: (k[0], k[1]))
    places = sorted({k[0] for k in keys})
    if ymd_str:
        from datetime import datetime
        ymd = datetime.strptime(ymd_str, "%Y%m%d").date()
    else:
        ymd = date.today()

    n_jra = sum(1 for p in places if eng.is_jra(p))
    if n_jra == len(places):
        kind_label, kind = "中央", "jra"
    elif n_jra == 0:
        kind_label, kind = "地方", "nar"
    else:
        kind_label, kind = "中央・地方", "all"
    title = (f"【{kind_label}競馬AI予想】"
             f"{ymd.year}年{ymd.month}月{ymd.day}日 {'・'.join(places)} 全レース予想")

    cards = []
    hits = fuku = graded = 0
    ev_invest = ev_ret = 0
    for key in keys:
        info = race_info.get(key, {})
        jra = eng.is_jra(key[0])
        ranked = eng.predict_race(races[key], info)
        conf = eng.confidence_label(ranked)
        payout = eng.TAKEOUT_RETURN_JRA if jra else eng.TAKEOUT_RETURN
        picks = eng.ev_bets(ranked, ev_threshold, payout)
        pb = paybacks.get(key) if paybacks else None
        card, _hit = race_card(key, info, ranked, conf, picks, pb)
        cards.append(card)
        if pb:
            graded += 1
            top = ranked[0]
            if top.umaban == pb["勝ち馬番"]:
                hits += 1
                fuku += 1
            elif top.umaban in pb.get("複勝馬番", []):
                fuku += 1
            for pk in picks:
                ev_invest += 100
                if pk["horse"].umaban == pb["勝ち馬番"]:
                    ev_ret += pb["単勝払戻"]

    parts = [f"<div style='max-width:720px;margin:0 auto;font-family:{SANS};color:{INK}'>"]
    parts.append(
        f"<p style='font-size:14px'>予想チャットくん(AIスコアリング)による本日の全レース予想です。"
        f"対象は {esc('、'.join(places))} の全{len(keys)}レース。"
        f"印・参考買い目・期待値買いをレースごとにまとめています。</p>")

    if graded:
        recov = ev_ret / ev_invest * 100 if ev_invest else 0
        parts.append(
            f"<div style='background:{PAPER};border:1.5px solid {TURF};border-radius:10px;"
            f"padding:14px;margin:14px 0;display:flex;gap:8px;justify-content:space-around;flex-wrap:wrap'>"
            f"{_stat('答え合わせ', f'{graded}R')}"
            f"{_stat('◎単勝的中', f'{hits}/{graded}')}"
            f"{_stat('◎3着以内', f'{fuku}/{graded}')}"
            f"{_stat('期待値買い回収率', f'{recov:.0f}%')}"
            f"</div>")
        title += "(結果あり)"

    parts.extend(cards)
    parts.append(
        f"<hr style='border:none;border-top:1px solid {LINE};margin:24px 0 12px'>"
        f"<p style='font-size:12px;color:{MUTE};line-height:1.7'>本記事の予想はプログラムによる"
        f"参考情報であり、的中を保証するものではありません。オッズ・出走状況は変動します。"
        f"馬券の購入は20歳以上・自己責任で、余裕資金の範囲でお楽しみください。</p></div>")

    return title, "\n".join(parts), ymd.strftime("%Y%m%d"), kind


# ============================================================
# WordPress REST API
# ============================================================

def wp_request(cfg, method, path, payload=None):
    url = cfg["site_url"] + "/wp-json/wp/v2" + path
    token = base64.b64encode(
        f"{cfg['username']}:{cfg['app_password']}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "User-Agent": UA}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"WordPress APIエラー {e.code}: {detail}") from e


def find_existing_post(cfg, slug):
    res = wp_request(cfg, "GET", f"/posts?slug={urllib.parse.quote(slug)}&status=publish,draft,future")
    return res[0]["id"] if isinstance(res, list) and res else None


def post_article(cfg, title, html, slug, status=None):
    payload = {"title": title, "content": html, "slug": slug,
               "status": status or cfg["status"]}
    if cfg.get("category_id"):
        payload["categories"] = [int(cfg["category_id"])]
    post_id = find_existing_post(cfg, slug)
    if post_id:
        res = wp_request(cfg, "POST", f"/posts/{post_id}", payload)
        return res.get("link", ""), "更新"
    res = wp_request(cfg, "POST", "/posts", payload)
    return res.get("link", ""), "新規投稿"


# ============================================================
# メイン
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="予想をWordPressに自動投稿")
    src_g = ap.add_mutually_exclusive_group(required=True)
    src_g.add_argument("--fetch", action="store_true",
                       help="JRAから当日の出馬表を取得して投稿")
    src_g.add_argument("--csv", type=str,
                       help="手元のhorselist CSV。フォルダ指定で当日ファイルを自動選択、"
                            "{today}プレースホルダも使用可")
    ap.add_argument("--results", action="store_true",
                    help="結果も取得/読込して答え合わせ付き記事に更新")
    ap.add_argument("--date", type=str, default=None,
                    help="対象日 YYYYMMDD(省略時は今日。--csvのフォルダ/{today}の解決にも使用)")
    ap.add_argument("--config", type=Path, default=None, help="wp_config.jsonのパス")
    ap.add_argument("--ev", type=float, default=100.0, help="期待値閾値%%")
    ap.add_argument("--draft", action="store_true", help="下書きとして投稿")
    ap.add_argument("--dry-run", action="store_true",
                    help="投稿せずHTMLをファイルに保存して確認")
    args = ap.parse_args()

    # --- データ準備 ---
    ymd = args.date or date.today().strftime("%Y%m%d")
    if args.fetch:
        import fetch_jra
        print(f"🌐 JRAから{ymd}の出馬表を取得します...")
        h_path, r_path, _ = fetch_jra.fetch_thisweek(".", target_date=ymd)
        if args.results:
            try:
                fetch_jra.fetch_results(".", target_date=ymd)
            except RuntimeError as e:
                print(f"⚠ 結果取得は失敗(予想のみで投稿します): {e}")
        h_paths = None
    if args.fetch:
        h_paths = [h_path]
    else:
        try:
            h_paths = resolve_csv(args.csv, ymd)
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        if len(h_paths) > 1:
            print(f"📂 {len(h_paths)}件のhorselistを検出(中央/地方それぞれ記事化します)")

    cfg = None
    if not args.dry_run:
        cfg = load_config(args.config)

    for h_path in h_paths:
        print(f"\n📂 使用CSV: {h_path}")
        cur_ymd = args.date or detect_ymd_from_name(h_path) or ymd

        horses = eng.load_horselist(h_path)
        rl = eng.find_sibling(h_path, "racelist")
        pb = eng.find_sibling(h_path, "payback")
        race_info = eng.load_racelist(rl) if rl else {}
        paybacks = eng.load_payback(pb) if (pb and args.results) else {}

        title, html, cur_ymd, kind = build_article(
            horses, race_info, paybacks, args.ev, cur_ymd)
        slug = f"keiba-yosou-{cur_ymd}-{kind}"
        print(f"📝 記事生成: {title}(slug: {slug})")

        if args.dry_run:
            out = Path(f"wp_preview_{cur_ymd}_{kind}.html")
            page = (f"<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
                    f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>{esc(title)}</title></head>"
                    f"<body style='margin:0;padding:24px 12px;background:#EFEBE2'>"
                    f"<h1 style='max-width:720px;margin:0 auto 16px;font-family:{SERIF};"
                    f"font-size:24px;color:{INK}'>{esc(title)}</h1>{html}</body></html>")
            out.write_text(page, encoding="utf-8")
            print(f"💾 dry-run: {out} に保存しました(投稿はしていません)")
            continue

        status = "draft" if args.draft else None
        link, action = post_article(cfg, title, html, slug, status)
        print(f"✅ WordPressへ{action}しました: {link or '(URL取得なし)'}")


if __name__ == "__main__":
    main()
