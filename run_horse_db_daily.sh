#!/bin/sh
# 馬データベース(horses.sqlite3)の毎日更新(Ubuntu/cron向け)
# 本日の地方競馬データを取得し、出走した馬(+初見の馬)だけをDBに反映します。
#
# cron登録例(毎日20:00に実行):
#   crontab -e
#   0 20 * * * /path/to/UmaLytics/run_horse_db_daily.sh >> /path/to/UmaLytics/horse_db_daily.log 2>&1
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
"$PYTHON" horse_db.py --daily
