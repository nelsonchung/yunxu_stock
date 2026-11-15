# Yunxu Stock｜股市觀察看板

部署於樹莓派的 Python 3 / SQLite 網站。開啟 [區域網路看板](http://192.168.8.235:8090/) 後，可用股票代碼或部分公司名稱搜尋台股上市、上櫃與美股標的；將股票放到自訂分類，在單股與多股格狀看板間切換。設定可調整每頁行列數，以及最近 1、3、6、12 或 24 個月的日收盤價，並暫停背景補齊。分類刪除後，股票會回到未分類。

這是一份供區域網路使用的共用清單，設定與股票儲存在 `state/stocks.sqlite3`。請勿把這個服務對外開放；服務沒有個別帳號。備份請使用 SQLite backup 指令或在停服務後複製資料庫。

## 資料來源與更新規則

- 台股：[TWSE OpenAPI](https://openapi.twse.com.tw/)、[TWSE 個股日成交資訊](https://www.twse.com.tw/zh/trading/historical/stock-day.html)、[TPEx OpenAPI](https://www.tpex.org.tw/openapi/)、[TPEx 個股日成交資訊](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html)。應用程式不連線至公開資訊觀測站（MOPS），也不跟隨重新導向。
- 美股：[Nasdaq Trader 股票名錄](https://www.nasdaqtrader.com/trader.aspx?id=symbollookup)供個人非商業搜尋；歷史日線使用 [Alpha Vantage](https://www.alphavantage.co/documentation/#daily)。沒有金鑰仍可搜尋和管理美股，加入後會顯示需要金鑰。一般 compact 回傳最近 100 個交易日，超過的期間可使用有 full 權限的方案；下列設定變更只需重啟服務。
- 名錄每日背景更新，輸入搜尋只查本機，不會對外逐字請求。歷史更新只有一個背景執行緒；兩次請求之間至少等 5 秒，同一歷史來源至少隔 60 秒。台灣各來源每滾動 24 小時至多 48 次歷史請求，美股至多 20 次。已完成的台股月份會快取，從近期開始逐月補齊。HTTP 403／429 暫停至少 24 小時並遵守更長的 Retry-After，其他錯誤退避；重啟會保留限速紀錄。
- 顯示的是未還原權息的日收盤價。看板不提供即時行情；月初、休市、剛上市和來源限制都會影響可用的日線筆數。

## 啟用美股日線

取得 [Alpha Vantage API 金鑰](https://www.alphavantage.co/support/#api-key) 後，在樹莓派上建立只有 root 可讀的設定檔，再透過終端機編輯：

```sh
sudo install -m 0600 /dev/null /etc/yunxu-stock.env   # 僅在檔案尚未建立時執行
sudoedit /etc/yunxu-stock.env
sudo systemctl restart yunxu-stock
```

在設定檔填入下列內容；如有 full 資料權限，另加 `ALPHA_VANTAGE_FULL=1`：

```ini
ALPHA_VANTAGE_API_KEY=<你的金鑰>
```

## 開發、驗收與維運

```sh
./scripts/check.sh
python3 -m stockwatch.server
```

驗收腳本需要 Python 3 與 Node.js；網站執行只需要 Python 3 和系統時區資料。腳本以暫存 SQLite 與替身行情來源驗證搜尋、分類、增刪、設定、行情解析及限流，不消耗外部 API 額度。工作約定與驗收條件見 [AGENTS.md](AGENTS.md)。

樹莓派以 systemd 啟動（設定在 `deploy/yunxu-stock.service`）：
```sh
systemctl status yunxu-stock
journalctl -u yunxu-stock -n 50
curl --fail http://192.168.8.235:8090/healthz
```
