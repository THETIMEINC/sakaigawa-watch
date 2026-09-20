# sakaigawa-watch

神奈川県藤沢市を流れる境川（境川橋観測所）の水位を10分おきに取得し、氾濫の危険が近づいた場合に Slack で通知するプロジェクトです。

## ⚠️ 免責事項

**本通知は補助情報です。** 避難の判断は必ず藤沢市・神奈川県の公式情報、気象庁の警報・注意報、避難情報に従ってください。本システムの取得・予測が遅延・停止・誤動作する可能性があります。

## 何をするか

- [神奈川県雨量水位情報](https://www.pref.kanagawa.jp/sys/suibou/web_general/suibou_joho/html/stage/10/p10202_13_3585_4_309.html)（境川橋観測所）から10分値の水位を取得
- 直近の水位から**線形トレンド外挿**で、各警戒水位への到達予測時刻（ETA）を算出
- [Open-Meteo](https://open-meteo.com/)（気象庁モデル）から今後3時間の降水量予測を取得し、通知の判断材料として添える
- 警戒水位を超えた・超えそう・急上昇している場合に Slack へ通知
- 取得した水位は `data/levels.csv` に蓄積し、将来のモデル改良に使う
- **平常時は間引き**: GitHub Actionsのcron自体は10分おきに起動するが、水位が水防団待機水位より0.5m以上低い平常時は30分に1回だけ実際のアクセス・判定・commitを行う（それ以外の起動はほぼ即終了）。県ページは常に直近約4時間分のデータを返すため、間引いてもデータの解像度は失われない。水位が上がって閾値に近づくと自動的に毎回（10分おき）の本実行に切り替わる

## 警戒水位（境川橋、公式基準）

| 段階 | 水位 |
|---|---|
| 水防団待機水位 | 4.00 m |
| 氾濫注意水位 | 4.50 m |
| 避難判断水位 | 5.20 m |
| 氾濫危険水位 | 5.65 m |

## データ出典

- 水位・基準水位: [神奈川県 雨量水位情報](https://www.pref.kanagawa.jp/sys/suibou/web_general/suibou_joho/html/stage/10/)
- 降水量予測: [Open-Meteo](https://open-meteo.com/)（気象庁モデル `jma_seamless`）
- 河川カメラ: [横浜市 河川監視カメラ](https://mizubousai.city.yokohama.lg.jp/river_camera/rc_details_now.html?wpc=546694)

## 手法の限界と今後の改善余地

現時点の予測は**直近30〜60分の水位に対する線形トレンド外挿のみ**です。以下は精度向上の余地として認識していますが、実績データによる検証が済むまでは意図的に導入していません。

- **上流観測所を使ったラグ回帰**: 境橋(501)・高鎌橋(307)・大清水橋(308)（いずれも境川）、神鋼橋(310)（支川・柏尾川）は同一URL体系で取得可能。上流→下流の伝播時間 τ を相互相関で推定し、より長いリードタイムのETAを出せる可能性がある
- **貯留関数法**: 降雨量から流出量・水位を推定する河川工学の標準的手法。パラメータのキャリブレーションに実績出水イベントのデータが必要
- **感潮成分の分離**: 境川橋が感潮区間の影響を受けるかは未検証。[気象庁潮位表（横浜）](https://www.data.jma.go.jp/gmd/kaiyou/data/db/tide/suisan/txt/)との相関を見て、必要なら水位から潮汐成分を除いた「出水成分」で判断する
- 降雨予測は現在、通知文に添えるのみで予測式には未使用

データが蓄積され次第、これらを段階的に導入する予定です。

## セットアップ

```bash
python3 main.py --dry-run   # Slackへ送信せず標準出力のみ
python3 main.py             # SLACK_WEBHOOK_URL が設定されていれば通知
python3 -m unittest discover -s tests -v
```

GitHub Actions では `secrets.SLACK_WEBHOOK_URL` に Slack Incoming Webhook の URL を設定してください。

```bash
gh secret set SLACK_WEBHOOK_URL --body "https://hooks.slack.com/services/..."
```

## 構成

```
main.py       # 統括：取得 → 保存 → 判定 → 通知 → 状態更新
fetch.py      # I/O：県ページ取得・パース、Open-Meteo取得
forecast.py   # 純粋計算：異常値除去・線形回帰・ETA算出・警戒判定
notify.py     # Slack送信・メッセージ整形
config.toml   # 観測所URL・しきい値・クールダウン等の設定
data/levels.csv  # 蓄積データ
state.json    # 通知済み状態（ヒステリシス・クールダウン・連続失敗カウント）
```

依存ライブラリはありません（Python 3.12 標準ライブラリのみ）。
