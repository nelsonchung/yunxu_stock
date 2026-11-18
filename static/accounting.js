"use strict";

const $ = (id) => document.getElementById(id);
const amounts = new Intl.NumberFormat("zh-TW", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const signedAmounts = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2, signDisplay: "exceptZero" });
const times = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const refresh = $("refresh-accounting");
const table = $("accounting-table");
const labels = ["股票代號", "方向", "庫存股數", "均價", "最新價", "未實現損益"];

function formatted(value, formatter = amounts, suffix = "") {
  return Number.isFinite(value) ? formatter.format(value) + suffix : "—";
}

function styledPnl(element, pnl, suffix = "") {
  element.textContent = formatted(pnl, signedAmounts, suffix);
  element.classList.toggle("accounting-gain", Number.isFinite(pnl) && pnl > 0);
  element.classList.toggle("accounting-loss", Number.isFinite(pnl) && pnl < 0);
}

function taipeiTime(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  let timestamp = value.trim().replaceAll("/", "-").replace(" ", "T");
  if (/^\d{4}-\d{2}-\d{2}$/.test(timestamp)) return timestamp;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(timestamp)) timestamp += "+08:00";
  const date = new Date(timestamp);
  return Number.isFinite(date.getTime()) ? times.format(date) : "";
}

function showText(element, text) {
  element.textContent = text;
  element.hidden = !text;
}

function positionRow(position) {
  const row = document.createElement("tr");
  row.setAttribute("role", "row");
  const direction = { Buy: "買進", Sell: "賣出" }[position.direction] ?? "—";
  const values = [
    position.code, direction, formatted(position.quantity, amounts, " 股"),
    formatted(position.avg_price), formatted(position.last_price), "",
  ];
  values.forEach((value, index) => {
    const cell = document.createElement("td");
    cell.setAttribute("role", "cell");
    if (!index) cell.className = "accounting-code";
    const label = document.createElement("span");
    label.className = "accounting-cell-label";
    label.setAttribute("aria-hidden", "true");
    label.textContent = labels[index];
    const amount = document.createElement("span");
    amount.textContent = value;
    if (index === 5) styledPnl(amount, position.pnl, " 元");
    cell.append(label, amount);
    row.append(cell);
  });
  return row;
}

function render(data) {
  const positions = Array.isArray(data.positions) ? data.positions : null;
  const balance = data.balance;
  $("accounting-balance").textContent = formatted(balance?.amount, amounts, " 元");
  styledPnl($("accounting-pnl"), data.unrealized_pnl, " 元");
  const asOf = taipeiTime(balance?.as_of);
  showText($("accounting-balance-date"), asOf ? `券商資料時間 ${asOf}` : "");
  showText($("accounting-balance-error"), balance ? "" : (data.errors?.balance || "暫時無法取得交割帳戶餘額。"));
  showText($("accounting-count"), positions ? `${positions.length} 筆庫存` : "");
  showText($("accounting-positions-status"), positions
    ? (positions.length ? "" : "目前沒有證券庫存。")
    : (data.errors?.positions || "暫時無法取得庫存及未實現損益。"));
  $("accounting-positions").replaceChildren(...(positions ?? []).map(positionRow));
  table.hidden = !positions?.length;
  const fetched = taipeiTime(data.fetched_at);
  $("accounting-status").textContent = fetched ? `最後查詢 ${fetched}（台北時間）` : "資料來源為永豐證券。";
}

async function load() {
  refresh.disabled = true;
  $("accounting-status").textContent = "正在查詢永豐證券帳務…";
  try {
    const response = await fetch("/api/accounting", {
      headers: { "X-Yunxu-Accounting": "1", Accept: "application/json" },
      cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) throw new Error();
    render(data);
  } catch {
    render({});
    $("accounting-status").textContent = "帳務查詢失敗，請重新查詢。";
  } finally {
    refresh.disabled = false;
  }
}

refresh.addEventListener("click", load);
load();
