#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JRA公式サイトから今週の出馬表を取得して horselist/racelist CSV を作るモジュール
================================================================================
標準ライブラリのみ(urllib + re + html.parser)で動作します。

使い方(単体実行):
    python fetch_jra.py                # 今週の全レースを取得 → jra_horselist.csv 等を出力
    python fetch_jra.py --place 東京   # 競馬場で絞り込み
    python fetch_jra.py --out C:\\data  # 出力先フォルダ指定
    python fetch_jra.py --debug        # 取得したHTMLを保存(構造変更時の調査用)

GUIからは「🌐 JRAから取得」ボタンで呼び出されます。

【重要な注意】
- JRA公式サイト(www.jra.go.jp)の利用規約・robots.txtを確認の上、
  個人利用の範囲でお使いください。公式データサービスはJRA-VANです。
- サーバー負荷防止のためリクエスト間に1.5秒の待機を入れています。
  この値を短くしないでください。
- サイトのHTML構造は予告なく変更されます。取得に失敗する場合は
  --debug でHTMLを保存し、パターン(本ファイル内の正規表現)を調整してください。
- JRADBページ(出馬表など)は doAction() と同じ POST方式(cname=...)で
  取得します。GETの ?CNAME= はパラメータエラーになります(2026年7月確認)。
- 出馬表から取得できるのは 馬番/馬名/性齢/騎手/負担重量/馬体重(増減)/
  単勝オッズ/人気 など。通算成績・騎手成績の列は空になります
  (予想アプリは列が空でも動作します。オッズがあれば期待値計算は実オッズを使用)。
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.jra.go.jp"
TOP_URL = BASE + "/keiba/"
# 出馬表トップのCNAME(トップページから自動探索するが、失敗時のフォールバック)
DENMA_TOP_CNAME_FALLBACK = "pw01dli00/F3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
      "KeibaYosouChat/1.0 (personal use)")
WAIT_SEC = 1.5  # リクエスト間隔(短くしないこと)

JRA_PLACES = ("札幌", "函館", "福島", "新潟", "東京", "中山",
              "中京", "京都", "阪神", "小倉")

SEX_PAT = re.compile(r"^(牡|牝|セ|せん|騸)(\d{1,2})$")
KATAKANA_PAT = re.compile(r"^[ァ-ヶー]{2,18}$")
KINRYO_PAT = re.compile(r"^[☆★▲△◇]?\d{2}(\.\d)?$")
WEIGHT_PAT = re.compile(r"^(\d{3,4})\s*[\(（]?\s*([+±\-−]?\d+)?\s*[\)）]?$")
ODDS_PAT = re.compile(r"^\d{1,4}\.\d$")


def log(msg):
    print(msg, flush=True)


def fetch(url, data=None, referer=None, debug_dir=None, tag=""):
    """GET/POST両対応の取得。dataを渡すとPOST(フォームエンコード)"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            html = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        html = raw.decode("utf-8", errors="replace")
    if debug_dir:
        Path(debug_dir).mkdir(exist_ok=True)
        fn = Path(debug_dir) / f"{tag or 'page'}_{int(time.time()*1000)}.html"
        fn.write_text(html, encoding="utf-8")
    time.sleep(WAIT_SEC)
    return html


def fetch_jradb(action_path, cname, debug_dir=None, tag=""):
    """JRADBページを取得する。
    JRAサイトは <form method="POST"><input name="cname"> のPOST送信方式
    (画面上のdoAction()と同じ)。GETの?CNAME=はパラメータエラーになる。"""
    html = fetch(BASE + action_path, data={"cname": cname},
                 referer=TOP_URL, debug_dir=debug_dir, tag=tag)
    if "パラメータエラー" in html:
        raise RuntimeError(
            f"JRAサイトがパラメータエラーを返しました(cname={cname})。\n"
            "CNAMEの有効期限切れか、送信方式が変更された可能性があります。")
    return html


def find_denma_top_cname(debug_dir=None):
    """競馬メニューのトップから出馬表トップのCNAMEを探す"""
    try:
        html = fetch(TOP_URL, debug_dir, "keiba_top")
        m = re.search(
            r"doAction\(\s*['\"]/JRADB/accessD\.html['\"]\s*,\s*['\"](pw01dli\w*/?\w*)['\"]",
            html)
        if m:
            return m.group(1)
        m = re.search(r"accessD\.html\?CNAME=(pw01dli[\w/]+)", html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"  ⚠ トップページ解析に失敗({e})。既定値を使用します")
    return DENMA_TOP_CNAME_FALLBACK


def find_race_cnames(top_html):
    """出馬表トップ(開催・レース一覧)からレースページのCNAMEを収集"""
    # doAction('/JRADB/accessD.html','pw01dde...') と
    # accessD.html?CNAME=pw01dde... の両形式に対応
    cnames = re.findall(
        r"(?:doAction\(\s*['\"]/JRADB/accessD\.html['\"]\s*,\s*['\"]|accessD\.html\?CNAME=)"
        r"(pw01dde\w+(?:/\w+)?)", top_html)
    seen, out = set(), []
    for c in cnames:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_kaisai_cnames(top_html):
    """出馬表トップに開催選択(pw01dli01等)しか無い場合の中間ページCNAME"""
    cnames = re.findall(
        r"(?:doAction\(\s*['\"]/JRADB/accessD\.html['\"]\s*,\s*['\"]|accessD\.html\?CNAME=)"
        r"(pw01dli\w+(?:/\w+)?)", top_html)
    seen, out = set(), []
    for c in cnames:
        if c != DENMA_TOP_CNAME_FALLBACK and c not in seen:
            seen.add(c)
            out.append(c)
    return out


class TableParser(HTMLParser):
    """ページ内の全<tr>をセルテキストのリストとして収集する汎用パーサ"""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None

    def handle_data(self, data):
        if self._skip == 0 and self._cell is not None:
            self._cell.append(data)


def _norm_num(s, pat=re.compile(r"[^\d.+\-]")):
    return pat.sub("", str(s).replace("±", "").replace("−", "-"))


# 2026年7月時点のJRA出馬表HTML構造(実ページで確認済み)に基づく抽出パターン
RACE_NAME_PAT = re.compile(r'class="race_name"[^>]*>([^<]{2,60})')
DATE_KAISAI_PAT = re.compile(
    r'class="cell date"[^>]*>\s*(\d{4})年(\d{1,2})月(\d{1,2})日[^<]*?'
    r'(\d+)回(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)(\d+)日')
COURSE_PAT = re.compile(
    r'([12]?,?\d{3})\s*<span class="unit">メートル</span>'
    r'\s*<span class="detail">（(芝|ダート)・(右|左|直線)[^）]*）</span>')
HASSO_PAT = re.compile(r"発走時刻：?\s*(?:<[^>]+>)*(\d{1,2})時(\d{2})分")
WEATHER_PAT = re.compile(r'class="cell weather"[^>]*>(?:[^<]|<span[^>]*>|</span>){0,40}?(晴|曇|雨|小雨|雪|小雪)')

UMA_HEAD_PAT = re.compile(
    r"^([ァ-ヶーA-Za-z]+)\s*"           # 馬名
    r"(?:(\d{1,4}\.\d)\s*)?"          # 単勝オッズ(未発売時なし)
    r"(?:[\(（](\d{1,2})番人気[\)）])?"  # 人気
)
SEIREKI_PAT = re.compile(r"[\(（](\d+)\.(\d+)\.(\d+)\.(\d+)[\)）]")  # 通算成績 a.b.c.d
SEX_CELL_PAT = re.compile(r"(牡|牝|セ|せん|騸)\s*(\d{1,2})\s*/")
KINRYO_CELL_PAT = re.compile(r"(\d{2}(?:\.\d)?)\s*kg")
BATAIJU_PAT = re.compile(r"(\d{3})\s*kg\s*[\(（]\s*([+±\-−]?\d+)\s*[\)）]")
CHAKU_PAT = re.compile(r"(\d{1,2})着")
SCRATCH_PAT = re.compile(r"取消|除外|出走取消")


def parse_race_page(html):
    """出馬表1レース分のHTMLから レース情報dict と 馬リスト を抽出
    (2026年7月時点の実ページ構造に基づく。--debugで保存したHTMLで検証済み)"""
    info = {}
    m = RACE_NAME_PAT.search(html)
    if m:
        info["レース名"] = m.group(1).strip()
    m = DATE_KAISAI_PAT.search(html)
    if m:
        info["年月日"] = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
        info["回次"], info["競馬場"], info["日次"] = m.group(4), m.group(5), m.group(6)
    m = COURSE_PAT.search(html)
    if m:
        info["距離"] = int(m.group(1).replace(",", ""))
        info["芝ダート"] = m.group(2)
        info["回り"] = m.group(3).replace("直線", "直")
    m = HASSO_PAT.search(html)
    if m:
        info["発走時刻"] = f"{int(m.group(1))}:{m.group(2)}"
    m = WEATHER_PAT.search(html)
    if m:
        info["天候"] = m.group(1)

    # レース番号: ヘッダーの「N回○○M日 XXレース」表記から(全文を対象)
    text_all = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    m = re.search(r"\d+回\s*(?:札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)\s*\d+日\s*(\d{1,2})\s*レース",
                  text_all)
    if m:
        info["レース番号"] = int(m.group(1))

    # --- 馬テーブル ---
    tp = TableParser()
    tp.feed(html)
    horses = []
    seen_umaban = set()
    for row in tp.rows:
        if len(row) < 4:
            continue
        # 馬番セル(小さい整数)と性齢セル(牡4/鹿 …)を特定
        umaban = None
        for c in row[:3]:
            if c.strip().isdigit() and 1 <= int(c) <= 18:
                umaban = int(c)
                break
        sex_idx = next((i for i, c in enumerate(row) if SEX_CELL_PAT.search(c)), None)
        if umaban is None or sex_idx is None or umaban in seen_umaban:
            continue

        # 馬名セル: 性齢セルの直前
        name_cell = row[sex_idx - 1] if sex_idx >= 1 else ""
        hm = UMA_HEAD_PAT.match(name_cell)
        if not hm:
            continue
        name = hm.group(1)
        odds = hm.group(2) or ""
        ninki = hm.group(3) or ""
        rm = SEIREKI_PAT.search(name_cell)
        record = "-".join(rm.groups()) if rm else ""

        sex_cell = row[sex_idx]
        sm = SEX_CELL_PAT.search(sex_cell)
        km = KINRYO_CELL_PAT.search(sex_cell)
        kinryo = km.group(1) if km else ""
        # 騎手: 斤量kgの後ろの文字列(スペース除去)
        jockey = ""
        if km:
            tail = sex_cell[km.end():]
            jm = re.match(r"\s*[☆★▲△◇]?\s*([\u4e00-\u9fffぁ-んァ-ヶー・.\s]{2,12})", tail)
            if jm:
                jockey = re.sub(r"\s+", "", jm.group(1))
        # 馬体重(当日発表。前日は無いことが多い)
        weight = wdiff = ""
        bm = BATAIJU_PAT.search(" ".join(row))
        if bm and 300 <= int(bm.group(1)) <= 700:
            weight, wdiff = bm.group(1), _norm_num(bm.group(2))

        # 過去走: 性齢セルより後ろのセルから着順を順に(前走→3走前)
        last3 = []
        for c in row[sex_idx + 1:]:
            cm = CHAKU_PAT.search(c)
            if cm and re.search(r"\d{4}年", c):
                last3.append(cm.group(1))
            if len(last3) >= 3:
                break
        last3 += [""] * (3 - len(last3))

        scratched = bool(SCRATCH_PAT.search(name_cell + " " + sex_cell))
        seen_umaban.add(umaban)
        horses.append({
            "馬番": umaban, "馬名": name,
            "性": sm.group(1), "齢": sm.group(2),
            "負担重量": kinryo, "騎手名": jockey,
            "馬体重": weight, "馬体重増減": wdiff,
            "オッズ": odds, "人気": ninki,
            "全成績": record,
            "前走着順": last3[0], "2走前着順": last3[1], "3走前着順": last3[2],
            "状態": "取消" if scratched else "出走",
        })
    return info, horses


CNAME_PAT = re.compile(r"pw01dde(\d{2})(\d{2})(\d{4})(\d{4})(\d{2})(\d{8})")


def cname_date(cname):
    """pw01dde…のcnameから開催日付(YYYYMMDD)を取り出す"""
    m = CNAME_PAT.match(cname)
    return m.group(6) if m else None


def cname_race_no(cname):
    """pw01dde…のcnameからレース番号を取り出す"""
    m = CNAME_PAT.match(cname)
    return int(m.group(5)) if m else 0


def fetch_thisweek(out_dir=".", place_filter=None, debug=False, progress=log,
                   target_date=None):
    """今週の出馬表を取得してCSVを出力。(horselist_path, racelist_path, レース数)を返す

    JRAの出馬表はURL固定でないため、開催選択ページのレースリンクを起点に
    各レースページ内のリンク(同開催の1〜12R・他場のレース)を芋づる式に
    辿って全レースを収集します(BFSクロール)。
    target_date: "YYYYMMDD"。省略時は本日以降の開催のみ取得。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = out_dir / "jra_debug" if debug else None

    progress("🌐 JRA公式サイトから出馬表を取得します(間隔1.5秒/リクエスト)")
    progress("   ※個人利用の範囲で。JRAの利用規約をご確認ください")

    top_cname = find_denma_top_cname(debug_dir)
    progress(f"📄 出馬表 開催選択ページを取得中... (cname={top_cname})")
    top_html = fetch_jradb("/JRADB/accessD.html", top_cname, debug_dir, "denma_top")

    today = date.today().strftime("%Y%m%d")
    min_date = target_date or today

    def date_ok(cname):
        d = cname_date(cname)
        if d is None:
            return True
        if target_date:
            return d == target_date
        return d >= min_date

    # --- BFSクロール ---
    queue = [c for c in find_race_cnames(top_html) if date_ok(c)]
    if not queue:
        # 開催選択にレース直リンクが無い場合は開催リンク(pw01dli系)を辿る
        for kc in find_kaisai_cnames(top_html)[:12]:
            try:
                khtml = fetch_jradb("/JRADB/accessD.html", kc, debug_dir, "kaisai")
            except RuntimeError as e:
                progress(f"  ⚠ {e}")
                continue
            queue.extend(c for c in find_race_cnames(khtml) if date_ok(c))
    if not queue:
        raise RuntimeError(
            "レースページのリンク(pw01dde…)が見つかりませんでした。\n"
            "開催日でない可能性、またはサイト構造が変更された可能性があります。\n"
            "--debug でHTMLを保存して調査してください。")

    visited = set()
    h_rows, r_rows = [], []
    done_keys = set()
    ok = 0
    MAX_PAGES = 90  # 3場×12R×2日+α の上限ガード

    progress(f"🏇 レースページを巡回します(起点{len(queue)}件)...")
    i = 0
    while queue and len(visited) < MAX_PAGES:
        cname = queue.pop(0)
        if cname in visited or not date_ok(cname):
            continue
        visited.add(cname)
        i += 1
        try:
            html = fetch_jradb("/JRADB/accessD.html", cname, debug_dir, f"race{i:02d}")
        except RuntimeError as e:
            progress(f"  ⚠ 取得失敗: {e}")
            continue
        # 新しいレースリンクをキューへ
        for c in find_race_cnames(html):
            if c not in visited and date_ok(c):
                queue.append(c)
        try:
            info, horses = parse_race_page(html)
        except Exception as e:
            progress(f"  ⚠ 解析失敗({cname}): {e}")
            continue
        place = info.get("競馬場", "")
        race_no = info.get("レース番号", 0) or cname_race_no(cname)
        if not place or not race_no or not horses:
            progress(f"  ⚠ レース情報を特定できずスキップ({cname})")
            continue
        if place_filter and place_filter not in place:
            continue
        key = (info.get("年月日", min_date), place, race_no)
        if key in done_keys:
            continue
        done_keys.add(key)
        ok += 1
        runners = [h for h in horses if h["状態"] == "出走"]
        progress(f"  ✅ {place}{race_no}R {info.get('レース名','')} "
                 f"({len(runners)}頭{'/' + str(len(horses) - len(runners)) + '頭取消' if len(horses) > len(runners) else ''})")
        ymd = info.get("年月日", min_date)
        r_rows.append({
            "競馬場": place, "競走年月日": ymd, "レース番号": race_no,
            "発走時刻": info.get("発走時刻", "").replace(":", ""),
            "レース名": info.get("レース名", ""),
            "芝ダート区分": info.get("芝ダート", ""), "回り": info.get("回り", ""),
            "距離": info.get("距離", ""), "天候": info.get("天候", ""),
            "頭数": len(runners),
        })
        for h in horses:
            h_rows.append({"競馬場": place, "競走年月日": ymd,
                           "レース番号": race_no, **h})

    if not h_rows:
        raise RuntimeError("出走馬を1頭も取得できませんでした。--debug で調査してください。")

    tag = target_date or today
    # 中央競馬は JRA_YYYY_MMDD_*.csv 形式で統一(地方は YYYYMMDD_*.csv)
    jtag = f"JRA_{tag[:4]}_{tag[4:6]}{tag[6:8]}" if len(tag) == 8 else f"JRA_{tag}"
    h_path = out_dir / f"{jtag}_horselist.csv"
    r_path = out_dir / f"{jtag}_racelist.csv"
    h_cols = ["競馬場", "競走年月日", "レース番号", "馬番", "馬名", "性", "齢",
              "騎手名", "負担重量", "馬体重", "馬体重増減", "オッズ", "人気",
              "全成績", "前走着順", "2走前着順", "3走前着順", "状態"]
    r_cols = ["競馬場", "競走年月日", "レース番号", "発走時刻", "レース名",
              "芝ダート区分", "回り", "距離", "天候", "頭数"]
    with open(h_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=h_cols)
        w.writeheader()
        w.writerows(h_rows)
    with open(r_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=r_cols)
        w.writeheader()
        w.writerows(r_rows)
    progress(f"💾 保存完了: {h_path.name}({len(h_rows)}頭) / {r_path.name}({ok}レース)")
    return h_path, r_path, ok



# ============================================================
# レース結果(払戻)の取得
# ============================================================
# 出馬表と同様のBFS方式で「レース結果」ページを巡回します。
# 結果ページのcnameは pw01sde… 形式(出馬表のdをsに置き換えた体系)を想定。
# ※結果ページ本体のHTML構造は未検証のため、失敗時は --debug のHTMLを
#   確認してパターンを調整してください。

RESULT_CNAME_PAT = re.compile(r"pw01sde(\d{2})(\d{2})(\d{4})(\d{4})(\d{2})(\d{8})")


def find_result_cnames(html):
    cnames = re.findall(
        r"(?:doAction\(\s*['\"]/JRADB/accessS\.html['\"]\s*,\s*['\"]|accessS\.html\?CNAME=)"
        r"(pw01sde\w+(?:/\w+)?)", html)
    seen, out = set(), []
    for c in cnames:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_result_top_cname(debug_dir=None):
    try:
        html = fetch(TOP_URL, debug_dir, tag="keiba_top_r")
        m = re.search(
            r"doAction\(\s*['\"]/JRADB/accessS\.html['\"]\s*,\s*['\"](pw01sli\w*/?\w*)['\"]",
            html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"  ⚠ トップページ解析に失敗({e})。既定値を使用します")
    return "pw01sli00/AF"


def parse_result_page(html):
    """結果ページから レース情報 / 着順上位 / 払戻 を抽出"""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    info = {}
    m = re.search(r"(\d+)回\s*(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)\s*(\d+)日\s*(\d{1,2})\s*レース", text)
    if m:
        info["競馬場"], info["レース番号"] = m.group(2), int(m.group(4))
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        info["年月日"] = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"

    # 着順(1〜3着の馬番): 「1着 枠 馬番 馬名…」風の並びを想定した2パターン
    chaku = {}
    for c, u in re.findall(r"(\d{1,2})\s*着\s*\d{1,2}\s*(\d{1,2})\s*[ァ-ヶー]", text):
        chaku.setdefault(int(c), int(u))
    if not chaku:
        tp = TableParser()
        tp.feed(html)
        for row in tp.rows:
            cells = [c for c in row if c]
            if len(cells) >= 4 and cells[0].isdigit() and 1 <= int(cells[0]) <= 18:
                nums = [c for c in cells[1:4] if c.strip().isdigit()]
                has_name = any(re.search(r"[ァ-ヶー]{3,}", c) for c in cells[1:6])
                if nums and has_name:
                    chaku.setdefault(int(cells[0]), int(nums[-1]))

    # 払戻: 「単勝 7 470円」「複勝 7 170円 8 210円 12 130円」等
    pays = {}
    m = re.search(r"単勝\s*(\d{1,2})\s*([\d,]+)\s*円", text)
    if m:
        pays["単勝組番"] = int(m.group(1))
        pays["単勝払戻金（円）"] = int(m.group(2).replace(",", ""))
    m = re.search(r"複勝((?:\s*\d{1,2}\s*[\d,]+\s*円){1,3})", text)
    if m:
        for i, (u, y) in enumerate(re.findall(r"(\d{1,2})\s*([\d,]+)\s*円", m.group(1))[:3], 1):
            pays[f"複勝組番{i}"] = int(u)
            pays[f"複勝払戻金{i}"] = int(y.replace(",", ""))
    return info, chaku, pays


def fetch_results(out_dir=".", place_filter=None, debug=False, progress=log,
                  target_date=None):
    """本日(または指定日)のレース結果を取得してpayback CSVを出力"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = out_dir / "jra_debug" if debug else None

    progress("🌐 JRAレース結果を取得します(間隔1.5秒/リクエスト)")
    top_cname = find_result_top_cname(debug_dir)
    progress(f"📄 結果 開催選択ページを取得中... (cname={top_cname})")
    top_html = fetch_jradb("/JRADB/accessS.html", top_cname, debug_dir, "result_top")

    today = date.today().strftime("%Y%m%d")
    tgt = target_date or today

    def date_ok(cname):
        m = RESULT_CNAME_PAT.match(cname)
        return (m.group(6) == tgt) if m else True

    queue = [c for c in find_result_cnames(top_html) if date_ok(c)]
    if not queue:
        raise RuntimeError(
            "結果ページのリンク(pw01sde…)が見つかりませんでした。\n"
            "まだ結果が出ていないか、cname体系が想定と異なる可能性があります。\n"
            "--debug で result_top のHTMLを保存して確認してください。")

    visited, rows, ok = set(), [], 0
    done_keys = set()
    i = 0
    while queue and len(visited) < 90:
        cname = queue.pop(0)
        if cname in visited or not date_ok(cname):
            continue
        visited.add(cname)
        i += 1
        try:
            html = fetch_jradb("/JRADB/accessS.html", cname, debug_dir, f"result{i:02d}")
        except RuntimeError as e:
            progress(f"  ⚠ 取得失敗: {e}")
            continue
        for c in find_result_cnames(html):
            if c not in visited and date_ok(c):
                queue.append(c)
        info, chaku, pays = parse_result_page(html)
        place = info.get("競馬場", "")
        m = RESULT_CNAME_PAT.match(cname)
        race_no = info.get("レース番号", 0) or (int(m.group(5)) if m else 0)
        if not place or not race_no or "単勝組番" not in pays:
            progress(f"  ⚠ 結果を特定できずスキップ({cname})")
            continue
        if place_filter and place_filter not in place:
            continue
        key = (place, race_no)
        if key in done_keys:
            continue
        done_keys.add(key)
        ok += 1
        progress(f"  ✅ {place}{race_no}R 勝ち馬{pays['単勝組番']}番 "
                 f"単勝{pays['単勝払戻金（円）']}円")
        rows.append({"競馬場": place, "競走年月日": info.get("年月日", tgt),
                     "レース番号": race_no,
                     "1着馬番": chaku.get(1, pays.get("単勝組番", "")),
                     "2着馬番": chaku.get(2, ""), "3着馬番": chaku.get(3, ""),
                     **pays})

    if not rows:
        raise RuntimeError("結果を1件も取得できませんでした。--debug で調査してください。")

    jtag = f"JRA_{tgt[:4]}_{tgt[4:6]}{tgt[6:8]}" if len(tgt) == 8 else f"JRA_{tgt}"
    path = out_dir / f"{jtag}_payback.csv"
    cols = ["競馬場", "競走年月日", "レース番号", "1着馬番", "2着馬番", "3着馬番",
            "単勝組番", "単勝払戻金（円）",
            "複勝組番1", "複勝払戻金1", "複勝組番2", "複勝払戻金2",
            "複勝組番3", "複勝払戻金3"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    progress(f"💾 保存完了: {path.name}({ok}レース)")
    return path, ok


def main():
    ap = argparse.ArgumentParser(description="JRA今週の出馬表を取得してCSV化")
    ap.add_argument("--out", type=Path, default=Path("."), help="出力先フォルダ")
    ap.add_argument("--place", type=str, default=None, help="競馬場で絞り込み(例: 東京)")
    ap.add_argument("--debug", action="store_true", help="取得HTMLを保存")
    ap.add_argument("--date", type=str, default=None,
                    help="取得対象日 YYYYMMDD(省略時は本日以降)")
    ap.add_argument("--results", action="store_true",
                    help="出馬表ではなくレース結果(払戻)を取得")
    args = ap.parse_args()
    try:
        if args.results:
            fetch_results(args.out, args.place, args.debug, target_date=args.date)
        else:
            fetch_thisweek(args.out, args.place, args.debug, target_date=args.date)
    except Exception as e:
        print(f"❌ 取得失敗: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
