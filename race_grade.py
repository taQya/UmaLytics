#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""レース名/クラス表記から大まかな「格」を数値化する。
=====================================================================
地方競馬のクラス表記(Ｃ４〜Ｃ１・Ｂ・Ａ/オープン)と重賞(Jpn1-3・G1-3・
無格付けの重賞)を一つの序数スケールに乗せ、「前走」と「今回のレース」の
格差を計算できるようにする。deba_table.py(出馬表の直近5走)と
horse_stats.py(horses.sqlite3の生涯レース履歴)の両方から呼ばれる共通部品。

表記ゆれの多い自由文字列から雑に判定するため、細かい格付け(Ａ１とＡ２の
違いなど)は無視する。判定できない場合は None を返し、呼び出し側はその
レースの補正をスキップする(=既存の挙動のまま。悪化を避けるため)。
"""

import re

# 高いほど格上。JpnI/GIを最上位、C4を最下位とする大まかな序数。
# (絶対値そのものに意味はなく、差分の符号・大きさだけを補正に使う)
LEVEL_GRADE1 = 9   # Jpn1 / G1
LEVEL_GRADE2 = 8   # Jpn2 / G2
LEVEL_GRADE3 = 7   # Jpn3 / G3
LEVEL_STAKES = 6   # 重賞(格付け表記なし・準重賞含む)
LEVEL_OPEN = 5     # オープン・Ａ級
LEVEL_B = 4
LEVEL_C1 = 3
LEVEL_C2 = 2
LEVEL_C3 = 1
LEVEL_C4 = 0

# 「ＪＲＡ」表記が本文に混ざると、末尾の「Ａ」がＡ級と誤検出されるため
# 判定前に除去する(交流重賞の「ＪＲＡ選定馬」等でよく登場する)。
_STRIP = ("ＪＲＡ", "JRA")

_TIER_NUM = {
    "I": 1, "II": 2, "III": 3,
    "1": 1, "2": 2, "3": 3, "１": 1, "２": 2, "３": 3,
}
_G_RE = re.compile(r"(?:JPN|G)\s*(I{1,3}|[1-3１２３])")


def grade_level(*texts):
    """grade列・レース名などのテキストからクラス格付けを推定する(概算)。
    判定できなければ None。"""
    s = "".join(t for t in texts if t)
    if not s:
        return None
    for tok in _STRIP:
        s = s.replace(tok, "")
    s = s.upper()

    m = _G_RE.search(s)
    if m:
        tier = _TIER_NUM.get(m.group(1))
        return {1: LEVEL_GRADE1, 2: LEVEL_GRADE2, 3: LEVEL_GRADE3}.get(tier, LEVEL_STAKES)
    if "重賞" in s:
        return LEVEL_STAKES
    if "Ａ" in s or "オープン" in s:
        return LEVEL_OPEN
    if "Ｂ" in s:
        return LEVEL_B
    if "Ｃ１" in s:
        return LEVEL_C1
    if "Ｃ２" in s:
        return LEVEL_C2
    if "Ｃ３" in s:
        return LEVEL_C3
    if "Ｃ４" in s:
        return LEVEL_C4
    return None


# ============================================================
# 「前走」と「今回」の格差による近走点の補正
# ============================================================
# 僅差とみなす着差(馬身)。ハナ〜クビ〜アタマ相当の僅差を想定。
NEAR_MISS_LENGTHS = 0.3
# 重賞級での僅差敗戦は「2着相当」まで評価を戻す目安。
NEAR_MISS_PTS = 4.0


def class_adjust(pts, chaku, margin, past_level, cur_level, strength=1.0):
    """近走1走ぶんの評価点(pts)を、前走と今回のクラス差で補正する。

      - 格下での好走(勝利含む)は割り引く(前走からの"昇格"挑戦を考慮)。
      - 重賞級以上での好走(格上相手)はやや高く評価する。
      - 重賞級以上での凡走は、着差が僅かなら「健闘」として評価を戻し、
        大敗でも「格上相手に流した」可能性を考えて罰点を緩和する。

    past_level / cur_level のどちらかが不明(None)、または strength<=0 の
    場合は pts をそのまま返す(=既存の挙動)。"""
    if pts is None or past_level is None or cur_level is None or strength <= 0:
        return pts
    delta = cur_level - past_level  # >0: 今回の方が格上(昇格しての挑戦)

    if pts > 0:
        if delta > 0:
            factor = max(0.35, 1.0 - 0.18 * delta * strength)
        elif delta < 0 and past_level >= LEVEL_STAKES:
            factor = min(1.6, 1.0 + 0.12 * (-delta) * strength)
        else:
            factor = 1.0
        return pts * factor

    if past_level >= LEVEL_STAKES:
        if margin is not None and margin <= NEAR_MISS_LENGTHS:
            bonus_pts = NEAR_MISS_PTS
            return pts + (bonus_pts - pts) * strength
        return pts * (1.0 - 0.5 * strength)

    return pts
