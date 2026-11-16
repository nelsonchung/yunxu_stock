# 鋆旭觀測站

部署於樹莓派的 Python 3 / SQLite 網站。開啟 [區域網路看板](http://192.168.8.235:8090/) 後，可用股票代碼或部分公司名稱搜尋台股上市、上櫃與美股標的；將股票放到自訂分類，在單股與多股格狀看板間切換。設定可調整每頁行列數，以及最近 1、3、6、12 或 24 個月的日收盤價，並暫停背景行情更新。分類刪除後，股票會回到未分類。

這是一份供區域網路使用的共用清單，設定與股票儲存在 `state/stocks.sqlite3`。請勿把這個服務對外開放；服務沒有個別帳號。備份請使用 SQLite backup 指令或在停服務後複製資料庫。

單股頁會顯示已載入的日線範圍，以及台股相關公司。清單優先顯示已整理的晶圓代工同行，再依交易所產業分類提供同產業公司。可直接加入自選，或切換到已追蹤的公司。

## 資料來源與更新規則

台股從 [TWSE OpenAPI](https://openapi.twse.com.tw/) 與 [TPEx OpenAPI](https://www.tpex.org.tw/openapi/) 每日累積盤後日線。個人非商業部署可使用 [FinMind](https://finmind.github.io/tutor/TaiwanMarket/Technical/) 補齊歷史的開高低收與成交量，供單股頁繪製 K 線與技術指標。美股使用 [Nasdaq Trader 股票名錄](https://www.nasdaqtrader.com/trader.aspx?id=symbollookup) 搜尋，以 [Alpha Vantage](https://www.alphavantage.co/documentation/#daily) 取得日線。看板顯示未還原權息的日行情。

- [資料來源與使用規則](docs/data-sources.md)
- [系統架構與維運](docs/architecture.md)

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
python3 -m stockwatch.server --offline --db state/dev.sqlite3
```

驗收腳本需要 Python 3 與 Node.js；網站執行只需要 Python 3 和系統時區資料。腳本以暫存 SQLite 與替身行情來源驗證搜尋、分類、增刪、設定、行情解析及限流，不消耗外部 API 額度。工作約定與驗收條件見 [AGENTS.md](AGENTS.md)。

樹莓派以 systemd 啟動（設定在 `deploy/yunxu-stock.service`）：
```sh
systemctl status yunxu-stock
journalctl -u yunxu-stock -n 50
curl --fail http://192.168.8.235:8090/healthz
```
