# Yunxu Stock 開發與驗收

- 先閱讀 README 與 [資料來源與使用規則](docs/data-sources.md)。Python 與 SQLite 後端在 `stockwatch/`，繁中介面在 `static/`，樹莓派服務在 `deploy/`。
- 改資料來源前查閱官方文件與授權；不能新增 MOPS 連線或將本地搜尋改成逐字對外請求。背景工作保持單一執行緒、允許來源清單、限速紀錄、Retry-After 與暫停設定。
- 不將 API 金鑰或實際看板資料寫入原始碼、測試、瀏覽器回應或紀錄。測試使用暫存 SQLite 與替身來源。
- 交付前執行 `./scripts/check.sh`。介面變更還要驗證瀏覽器中的代碼及名稱搜尋、新增／移除、分類、單股／格狀及切換頁。部署後檢查 systemd 和 `/healthz`；回報實際可用的資料來源及尚無法驗證的部分。
