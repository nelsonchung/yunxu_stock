# 鋆旭觀測站

部署於樹莓派的 Python 3 / SQLite 網站。開啟 [區域網路看板](http://192.168.8.235:8090/) 後，可用股票代碼或部分公司名稱搜尋台股上市、上櫃與美股標的；將股票放到自訂分類，在單股與多股格狀看板間切換。設定可調整每頁行列數，查看最近 1、3、6、12、24 個月或全部可用日線，並暫停背景行情更新。分類刪除後，股票會回到未分類。

這是一份供區域網路使用的共用清單，設定與股票儲存在 `state/stocks.sqlite3`。請勿把這個服務對外開放；服務沒有個別帳號。備份請使用 SQLite backup 指令或在停服務後複製資料庫。

網格與單股的「當日行情」會顯示最新可用交易日的開高低收、漲跌與成交量。連接永豐金證券 Shioaji 後，交易時段可查看台股盤中報價。單股的「技術線圖」可查看歷史走勢與指標，也可使用「自訂日期」縮小區間。單股頁可直接加入或切換到同產業公司。

## 資料來源與更新規則

台股從 [TWSE OpenAPI](https://openapi.twse.com.tw/) 與 [TPEx OpenAPI](https://www.tpex.org.tw/openapi/) 每日累積盤後日線。個人非商業部署可使用 [FinMind](https://finmind.github.io/tutor/TaiwanMarket/Technical/) 補齊歷史的開高低收與成交量，供單股頁繪製 K 線與技術指標。美股使用 [Nasdaq Trader 股票名錄](https://www.nasdaqtrader.com/trader.aspx?id=symbollookup) 搜尋，以 [Alpha Vantage](https://www.alphavantage.co/documentation/#daily) 取得日線。看板顯示未還原權息的日行情。

- [資料來源與使用規則](docs/data-sources.md)
- [系統架構與維運](docs/architecture.md)

## 開啟帳務頁

帳務頁顯示永豐證券庫存、未實現損益與交割帳戶餘額。它只監聽樹莓派本機介面，請在看盤的電腦建立 SSH 通道後，點看板的「帳務」或開啟 [帳務頁](http://127.0.0.1:8094/accounting)。

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8094:127.0.0.1:8094 nelson@192.168.8.235
```

保持這個終端機連線。帳務資料在開啟頁面或點選「重新查詢」時讀取，未實現損益採券商回傳值。帳務服務與看板行情使用不同的金鑰，帳務金鑰留在樹莓派的受保護設定中。

## 啟用台股盤中行情

先在永豐金證券理財網的「個人服務 → API 管理」申請 Shioaji 行情金鑰。依官方說明在樹莓派啟動 Shioaji 本機服務，確認它只監聽 `127.0.0.1` 且使用正式行情。金鑰與密鑰保存在 Shioaji 自己的設定中。看板只使用行情訂閱。[金鑰申請](https://sinotrade.github.io/zh/tutor/prepare/token/) · [本機服務設定](https://sinotrade.github.io/zh/env_setup/other/)

純看盤使用具「行情／資料」與「正式環境」權限的金鑰。Shioaji 的下單憑證無需啟用。若本機服務啟用了 HTTP 驗證，透過 systemd 的 `LoadCredential=` 將其 Bearer 驗證內容提供為 `shioaji-http-token`。金鑰檔、驗證檔與憑證放在專案之外。

樹莓派的行情服務使用官方 Shioaji Python SDK 接收成交與五檔，將行情轉給看板。部署設定在 `deploy/yunxu-shioaji-feed.conf`，SDK 安裝在獨立的 `/opt/yunxu-shioaji-feed` 虛擬環境。背景更新開啟時，盤中會主動收集自選股成交並將分線存入 SQLite，供稍晚開頁或重整時查看。分時圖只畫有成交資料的分鐘。

在樹莓派執行 `sudoedit /etc/yunxu-stock.env`，加入本機 Shioaji 的實際埠號，儲存後執行 `sudo systemctl restart yunxu-stock`。以下用 8091 示意，可選未被其他服務佔用的埠。

```ini
SHIOAJI_PORT=8091
```

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
