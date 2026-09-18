
# 地方競馬用

### レース情報取得

``` sh
python3 download_race_data.py
```

### 出馬表データ取得(CSVに無い過去5走など)

``` sh
python deba_table.py ./horselist/20260705_horselist.csv
```

### 予想

``` sh
python post_wordpress.py --csv ./horselist/20260705_horselist.csv

```

※ `post_wordpress.py` は地方のCSVなら未取得ぶんの出馬表データを自動で取りに行きます。

### 結果

### 馬DB(horses.sqlite3)の毎日更新(Ubuntu)

依存はPython標準ライブラリのみ(`requests` があれば使うが無くても `urllib` で動く)。
Python 3.8以降であれば追加インストール不要。

``` sh
git clone <このリポジトリのURL> UmaLytics
cd UmaLytics

# 動作確認(手動実行)
python3 horse_db.py --daily
```

cronで毎日自動実行する場合:

``` sh
chmod +x run_horse_db_daily.sh
crontab -e
```

crontabに以下を追記(毎日20:00に実行する例):

``` cron
0 20 * * * /path/to/UmaLytics/run_horse_db_daily.sh >> /path/to/UmaLytics/horse_db_daily.log 2>&1
```

`horses.sqlite3` と `horselist/` はgit管理外なので、初回はDBが空の状態から始まり、
毎日の実行を重ねるごとに出走馬のデータが蓄積されていきます。

