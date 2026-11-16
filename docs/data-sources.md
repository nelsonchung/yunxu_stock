# 鋆旭觀測站資料來源與取得規則

維護基準日期為 2026 年 11 月 15 日。台股盤後資料採證交所與櫃買中心公開的 OpenAPI，個人非商業部署可啟用 FinMind 補齊歷史日線。美股使用 Nasdaq Trader 名錄及 Alpha Vantage 日線。資料使用範圍依各來源當時的條款與資料集授權。

## 對外端點與用途

程式只接受下表的 HTTPS 主機和路徑，且拒絕重新導向。

| 來源 | 允許的主機與路徑 | 用途與授權依據 |
| --- | --- | --- |
| 臺灣證券交易所（TWSE） | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | 上市名錄與每日收盤價。[證交所 OpenAPI](https://openapi.twse.com.tw/) · [上市個股日成交資訊資料集](https://data.gov.tw/dataset/11549) |
| 臺灣證券交易所（TWSE） | `openapi.twse.com.tw/v1/opendata/t187ap03_L` | 上市公司產業別與實收資本額。[證交所 OpenAPI](https://openapi.twse.com.tw/) · [上市公司基本資料集](https://data.gov.tw/dataset/18419) |
| 證券櫃檯買賣中心（TPEx） | `www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes` | 上櫃名錄與每日收盤價。[櫃買中心 OpenAPI](https://www.tpex.org.tw/openapi/) · [上櫃股票收盤行情資料集](https://data.gov.tw/dataset/11371) |
| 證券櫃檯買賣中心（TPEx） | `www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O` | 上櫃公司產業別與實收資本額。實際連線主機是櫃買中心。[櫃買中心 OpenAPI](https://www.tpex.org.tw/openapi/) · [上櫃公司基本資料集](https://data.gov.tw/dataset/25036) |
| FinMind | `api.finmindtrade.com/api/v4/data` | 台股 `TaiwanStockPrice`，依自選股票與設定期間補開高低收及成交量。需明確設為個人非商業使用。[資料集](https://finmind.github.io/tutor/TaiwanMarket/Technical/) · [方案用途](https://finmind.github.io/Pricing/) · [使用條款](https://finmind.github.io/PrivacyPolicy/) |
| Nasdaq Trader | `www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt` | 美股名錄，本專案用於自選股的內部非商業查詢。[Nasdaq Trader 名錄用途與下載](https://www.nasdaqtrader.com/trader.aspx?id=symbollookup) · [Nasdaq Trader 公告列出的名錄檔案](https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2026-64) |
| Alpha Vantage | `www.alphavantage.co/query` | 美股 `TIME_SERIES_DAILY`，需 API 金鑰。使用期間與次數依實際方案。[日線 API 文件](https://www.alphavantage.co/documentation/#daily) · [服務條款](https://www.alphavantage.co/terms_of_service/) · [API 額度說明](https://www.alphavantage.co/support/api-key/) |

政府開放資料應依授權條款標示資料提供者。對外分享、公開重製或商業使用前，應確認實際使用的每項資料是否涵蓋該用途，並分開確認 Nasdaq Trader 與 Alpha Vantage 的授權。[政府資料開放授權條款第 1 版](https://data.gov.tw/license) · [證交所網站使用條款](https://www.twse.com.tw/zh/terms/use.html)

公開資訊觀測站（MOPS）主要用於公司申報財報、營收和重大訊息。交易行情使用表中資料來源，MOPS 主機與原本兩個交易所網站個股歷史查詢路徑都不在允許清單。原本的收盤價快取仍可查閱，台股盤後日線持續由兩交易所 OpenAPI 累積，FinMind 依設定補齊當時可取得的歷史資料。[證交所服務介紹](https://wwwc.twse.com.tw/zh/about/company/service.html)

樹莓派目前經站主確認為個人非商業使用，在 `/etc/yunxu-stock.env` 設定 `FINMIND_HISTORY=1`。新部署預設停用。對第三方、客戶或外網開放前須另確認授權，包含已存快取的使用範圍。[FinMind 資料授權](https://finmind.github.io/Disclaimer/)

## 排程、限流與退避

以下數值是程式自行設定的上限。若官方條款或回應要求更嚴格的限制，維護者應以官方限制為準。

| 規則 | 目前行為 |
| --- | --- |
| 執行方式 | 單一背景執行緒。所有對外請求的開始時間至少相隔 5 秒，同一更新工作最短間隔 60 秒。 |
| 台股日線和名錄 | 每市場共用同一批 OpenAPI 資料。平日於台北時間 18:15 查詢，若來源仍只有較早交易日便於 20:30 再查一次。這是程式排程，實際發布時間由來源決定。每個市場滾動 24 小時最多 3 次嘗試。 |
| 台股產業資料 | 成功後等待 7 天，滾動 24 小時最多 3 次嘗試。 |
| 美股名錄 | 成功後等待 24 小時，滾動 24 小時最多 3 次嘗試。 |
| 美股日線 | 有金鑰才請求，本程式滾動 24 小時最多 20 次。應把同一金鑰在其他程式用掉的次數也計入供應商整體額度。 |
| FinMind 台股歷史日線 | 啟用後每次查一檔自選股票。選「全部」時從官方資料集起始日 1994-10-01 查詢，各股以來源有資料的日期為準。首次追蹤或擴大期間會排入更新，之後每七天只重新查詢近期未涵蓋的日期並重疊七天。本程式滾動 24 小時最多 12 次。 |
| HTTP 403 或 429 | 來源與其同一主機上的請求至少冷卻 24 小時。若回應的 `Retry-After` 要求更久，採較長時間。 |
| 其他錯誤 | 依連續失敗次數延長等待。回應設有讀取逾時與大小上限，失敗時保留已存資料。 |

請求計數及冷卻狀態寫入 SQLite。重啟服務不會重新取得請求額度。瀏覽器重新整理、搜尋或切換股票不會立即對外發出行情請求。

## 暫停與端點維護

看板設定中的停用開關會暫停台股盤後行情寫入、FinMind 補歷史，以及 Alpha Vantage 日線請求。名錄與產業資料照各自排程更新。若需完全停止本程式的對外請求，請停止服務。

```sh
sudo systemctl stop yunxu-stock
systemctl is-active yunxu-stock
```

新增端點前，應核對該路徑的官方文件、用途授權、存取限制及標示方式，更新允許清單和本頁，再用測試替身驗證來源限制。遇到長期拒絕、官方條款變更或無法確認的存取限制，先停止該來源的取用，再查原始文件或向資料供應商確認。
