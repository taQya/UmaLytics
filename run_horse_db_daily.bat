@echo off
rem 馬データベース(horses.sqlite3)の毎日更新
rem 本日の地方競馬データを取得し、出走した馬(+初見の馬)だけをDBに反映します。
rem タスクスケジューラ登録例:
rem   schtasks /create /tn "KeibaHorseDBDaily" /tr "E:\git\keiba\run_horse_db_daily.bat" /sc daily /st 20:00
cd /d %~dp0
python horse_db.py --daily
