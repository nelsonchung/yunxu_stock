# 鋆旭觀測站技術架構與維運

鋆旭觀測站是部署在樹莓派上的個人自選股網站。繁體中文介面支援台股上市、上櫃和美股搜尋，並提供分類、單股與多股檢視，以及台股相關公司。正式環境使用 Python 3 標準函式庫與 SQLite。Node.js 用於開發檢查。

## 元件與資料流

| 元件 | 路徑 | 職責 |
| --- | --- | --- |
| 瀏覽器介面 | `static/` | 自選清單、搜尋、分類、歷史日線、設定與相關公司 |
| HTTP 伺服器 | `stockwatch/server.py` | 提供靜態檔案與同來源 JSON API |
| SQLite 存取 | `stockwatch/store.py` | 儲存自選清單、名錄、產業資料、行情與更新狀態 |
| 背景更新 | `stockwatch/worker.py` | 在單一執行緒安排來源請求、解析回應並更新 SQLite |
| systemd 服務 | `deploy/yunxu-stock.service` | 在掛載專案磁碟與網路就緒後啟動服務 |

瀏覽器送出搜尋時，後端查詢 SQLite 名錄。加入股票會寫入自選清單，由背景工作依既有排程處理行情。瀏覽器取得日線、追蹤名單與相關公司時也只讀本機資料。外部市場資料的端點、授權依據與限流策略詳見[資料來源與取得規則](data-sources.md)。

台股相關公司先列已整理的晶圓代工同行，再依交易所分類列出同產業、名錄中仍可追蹤的公司。同產業排序使用實收資本額，合計最多八筆。設定中的月數決定圖表查詢範圍，實際交易日數取決於資料庫已存的日線。

單股技術分析以已存日線產生日、週、月三種週期。週線以週一為起點，月線依曆月聚合。均線與 MACD 使用收盤價，K 線、成交量與 KD 需要對應欄位。舊資料只有收盤價時保留該值，以及其他欄位的空缺。指標依所選週期計算，週期樣本不足時該指標保留空值。

## HTTP API

| 方法與路徑 | 功能 |
| --- | --- |
| `GET /` | 開啟看板。另提供 `/app.js` 和 `/style.css`。 |
| `GET /api/state` | 取得 `settings`、`categories`、`stocks` 及 `us_configured`。各股的 `history` 為 `[date, close]`，`candles` 為 `[date, open, high, low, close, volume]` 的完整日線。另含分類、狀態與相關公司。 |
| `GET /api/search?q=2330&market=TWSE` | 搜尋代號或公司名，最多回傳 12 筆。市場可省略，也可指定 `TWSE`、`TPEX` 或 `US`。 |
| `POST /api/action` | 傳送 JSON 操作自選清單、分類或設定。成功時回傳 `{"ok":true}`。 |
| `GET /healthz` | 檢查網站能否讀取 SQLite 設定表。外部行情可用性須另查來源狀態或服務日誌。 |

`POST /api/action` 可用的操作如下。

| `action` | 搭配欄位 |
| --- | --- |
| `add_stock` | `market`、`symbol`、選填 `category_id` |
| `remove_stock` | `id` |
| `move_stock` | `id`、`category_id`，傳 `null` 移回未分類 |
| `add_category` | `name` |
| `remove_category` | `id`，原分類內的股票移回未分類 |
| `settings` | `months` 為 0（全部）或 1、3、6、12、24，`rows`、`columns` 各為 1 至 6，`enabled` 為布林值 |

服務使用 HTTP，部署在可信任的區域網路。這是一份共用清單，網路內可觸及站點的使用者能修改同一份資料。伺服器檢查允許的 `Host`，也檢查帶有 `Origin` 的寫入請求是否來自同一來源。

## SQLite 與備份

正式資料庫位於專案目錄下的 `state/stocks.sqlite3`，使用 WAL 模式。

- `settings`、`stocks`、`categories` 儲存使用者設定與自選清單。
- `catalog`、`industries` 儲存搜尋名錄與產業資料。
- `prices` 以市場、代號、交易日期識別一筆行情，儲存開高低收、成交量及來源。資料庫升級會保留既有收盤價。FinMind 可補足只有收盤價的紀錄，完整的官方行情優先。
- `sources`、`requests` 保存來源冷卻、退避與請求紀錄，重啟後仍有效。

線上備份使用 SQLite 的 `.backup` 或 Python `sqlite3.Connection.backup()`。直接複製資料庫前應先停止服務，避免遺漏 WAL 中尚未寫回的資料。備份包含自選清單與行情快取，存放位置須符合原資料來源條款。

## 開發與部署驗證

在專案根目錄執行下列指令。開發時可改用測試資料庫，避免動到正式資料。

```sh
./scripts/check.sh
python3 -m stockwatch.server --offline --db state/dev.sqlite3
```

`--offline` 關閉背景執行緒，開發用網站只讀寫本機資料。`scripts/check.sh` 執行 Python 編譯、單元測試、JavaScript 語法與 Git 差異檢查。自動測試使用暫存 SQLite 和測試替身。介面變更後，另外以瀏覽器檢查代碼與名稱搜尋、自選股增刪、分類、單股與多股切換及相關公司加入流程。

正式服務名稱為 `yunxu-stock`，由 `nelson` 執行，工作目錄為 `/media/nelson/work-disk/web-site/yunxu_stock`。程式更新後以 systemd 重啟，並確認區域網路可讀取資料。

```sh
sudo systemctl restart yunxu-stock
systemctl is-active yunxu-stock
curl --fail http://192.168.8.235:8090/healthz
journalctl -u yunxu-stock -n 50 --no-pager
```

美股行情金鑰放在 `/etc/yunxu-stock.env`，檔案權限限制為 root 可讀。更新金鑰後須重啟服務。未設定金鑰時，Nasdaq Trader 名錄仍可用於搜尋，Alpha Vantage 日線等待啟用。
