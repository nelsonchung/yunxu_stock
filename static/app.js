"use strict";

const byId = (id) => document.getElementById(id);
const marketNames = { TWSE: "台股上市", TPEX: "台股上櫃", US: "美股" };
const filters = { market: "", category: "", view: "grid", focused: "", page: 0 };
const priceFormat = new Intl.NumberFormat("zh-TW", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const changeFormat = new Intl.NumberFormat("zh-TW", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "exceptZero",
});
const volumeFormat = new Intl.NumberFormat("zh-TW");
const quoteDateFormats = ["Asia/Taipei", "America/New_York"].map((timeZone) =>
  new Intl.DateTimeFormat("zh-TW", { year: "numeric", month: "2-digit", day: "2-digit", timeZone }));
let state;
let quoteDates = [];
let revision = 0;
let pendingWrites = 0;
let loadPending = false;

function element(tag, className, label) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (label !== undefined) node.textContent = label;
  return node;
}

function button(className, label, click) {
  const result = element("button", className, label);
  result.type = "button";
  result.addEventListener("click", click);
  return result;
}

async function request(url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("暫時無法連線，請稍後再試。");
  }
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "操作失敗，請稍後再試。");
  return data;
}

function showError(node, error) {
  node.textContent = error ? error.message || "操作失敗，請稍後再試。" : "";
  node.hidden = !error;
}

function announce(message) {
  byId("announcement").textContent = message;
}

function currentQuoteDates() {
  const now = new Date();
  return quoteDateFormats.map((formatter) => {
    const { year, month, day } = Object.fromEntries(
      formatter.formatToParts(now).map(({ type, value }) => [type, value]));
    return `${year}-${month}-${day}`;
  });
}

async function loadState({ quiet = false } = {}) {
  if (quiet && (loadPending || pendingWrites || document.hidden || document.querySelector("dialog[open]") ||
    document.activeElement?.matches("#board select"))) return;
  const version = ++revision;
  loadPending = true;
  try {
    const next = await request("/api/state");
    if (version !== revision) return;
    const dates = currentQuoteDates();
    if (!quiet || dates.some((date, index) => date !== quoteDates[index]) ||
        JSON.stringify(next) !== JSON.stringify(state)) {
      state = next;
      quoteDates = dates;
      render();
    }
    showError(byId("page-error"), null);
  } catch (error) {
    if (version === revision && (!quiet || !state)) showError(byId("page-error"), error);
  } finally {
    if (version === revision) loadPending = false;
  }
}

async function write(action, errorNode, message) {
  pendingWrites += 1;
  showError(errorNode, null);
  try {
    await request("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action),
    });
    await loadState();
    announce(message);
    return true;
  } catch (error) {
    showError(errorNode, error);
    return false;
  } finally {
    pendingWrites -= 1;
  }
}

function setOptions(select, options, selected = "") {
  select.replaceChildren(...options.map(({ value, label }) => new Option(label, value)));
  if ([...select.options].some((option) => option.value === String(selected))) {
    select.value = selected;
  }
}

function categoryOptions() {
  return [{ value: "", label: "未分類" }, ...state.categories.map(({ id, name }) => ({
    value: id, label: name,
  }))];
}

function categoryMatch(stock, id) {
  return id === "" || String(stock.category_id ?? "none") === id;
}

function renderFilters(container, items, key) {
  const previouslyFocused = document.activeElement?.dataset.filterValue;
  container.replaceChildren(...items.map(({ value, label, count }) => {
    const control = button("filter-button", undefined, () => {
      filters[key] = value;
      filters.page = 0;
      render();
    });
    control.dataset.filterValue = value;
    control.setAttribute("aria-pressed", String(filters[key] === value));
    control.append(element("span", "", label), element("span", "count", count));
    return control;
  }));
  if (previouslyFocused !== undefined && !document.activeElement.closest(".filters")) {
    [...container.children].find((item) => item.dataset.filterValue === previouslyFocused)?.focus();
  }
}

function render() {
  const { stocks, categories, settings } = state;
  if (filters.category !== "none" && !categories.some(({ id }) => String(id) === filters.category)) {
    filters.category = "";
  }
  const markets = [{ value: "", label: "全部市場" }, ...Object.entries(marketNames).map(([value, label]) => ({ value, label }))];
  renderFilters(byId("market-filters"), markets.map((item) => ({
    ...item,
    count: stocks.filter((stock) => (!item.value || stock.market === item.value) && categoryMatch(stock, filters.category)).length,
  })), "market");
  const categoryFilters = [{ value: "", label: "所有分類" }, { value: "none", label: "未分類" },
    ...categories.map(({ id, name }) => ({ value: String(id), label: name }))];
  renderFilters(byId("category-filters"), categoryFilters.map((item) => ({
    ...item,
    count: stocks.filter((stock) => categoryMatch(stock, item.value) && (!filters.market || stock.market === filters.market)).length,
  })), "category");

  byId("sync-dot").classList.toggle("active", settings.enabled);
  byId("sync-status").textContent = settings.enabled ? "背景更新已開啟" : "背景更新已暫停";
  const visible = stocks.filter((stock) => categoryMatch(stock, filters.category) &&
    (!filters.market || stock.market === filters.market));
  byId("us-notice").hidden = state.us_configured || !visible.some(({ market }) => market === "US");
  const historySummary = settings.months === 0
    ? "顯示全部已存日線"
    : `顯示近 ${settings.months} 個月內已存的日線`;
  const summary = filters.view === "focus" ? historySummary : "盤後行情，非盤中即時報價";
  byId("view-summary").textContent = `${visible.length} 檔股票 · ${summary}`;
  renderBoard(visible);
}

function renderBoard(visible) {
  const focus = filters.view === "focus";
  const board = byId("board");
  const relatedFocus = document.activeElement?.dataset.relatedKey;
  const technicalFocus = document.activeElement?.dataset.technicalKey;
  const { rows, columns } = state.settings;
  const perPage = rows * columns;
  const pages = Math.max(1, Math.ceil(visible.length / perPage));
  filters.page = Math.min(filters.page, pages - 1);
  if (!visible.some(({ id }) => String(id) === filters.focused)) {
    filters.focused = String(visible[0]?.id ?? "");
  }
  byId("grid-view").setAttribute("aria-pressed", String(!focus));
  byId("focus-view").setAttribute("aria-pressed", String(focus));
  byId("focus-picker").hidden = !focus || !visible.length;
  byId("open-layout").hidden = focus;
  if (focus) setOptions(byId("focused-stock"), visible.map(({ id, symbol, name }) => ({
    value: id, label: `${symbol} ${name}`,
  })), filters.focused);
  board.classList.toggle("is-focus", focus);
  board.style.setProperty("--columns", Number(columns));
  byId("pagination").hidden = focus || pages < 2;
  byId("page-number").textContent = `${filters.page + 1} / ${pages}`;
  byId("previous").disabled = filters.page === 0;
  byId("next").disabled = filters.page >= pages - 1;
  const display = focus ? visible.filter(({ id }) => String(id) === filters.focused) :
    visible.slice(filters.page * perPage, (filters.page + 1) * perPage);
  for (const chart of board.querySelectorAll("canvas")) chartObserver.unobserve(chart);
  const cards = display.length ? display.map(stockCard) : [emptyBoard()];
  if (focus && display.length) cards.push(relatedCompanies(display[0]));
  board.replaceChildren(...cards);
  for (const chart of board.querySelectorAll("canvas")) chartObserver.observe(chart);
  if (relatedFocus) {
    [...board.querySelectorAll("[data-related-key]")]
      .find((control) => control.dataset.relatedKey === relatedFocus)?.focus({ preventScroll: true });
  }
  if (technicalFocus) {
    [...board.querySelectorAll("[data-technical-key]")]
      .find((control) => control.dataset.technicalKey === technicalFocus)?.focus({ preventScroll: true });
  }
}

function emptyBoard() {
  const empty = element("div", "empty");
  const hasStocks = state.stocks.length > 0;
  empty.append(
    element("h2", "", hasStocks ? "這裡沒有股票" : "建立你的自選清單"),
    element("p", "", hasStocks ? "切換市場或分類，或將股票加入清單。" : "輸入股票代碼或公司名稱，加入第一檔股票。"),
    button("button button-primary", "＋ 新增股票", openStockDialog),
  );
  return empty;
}

function focusStock(stock) {
  if (filters.market && filters.market !== stock.market) filters.market = "";
  if (!categoryMatch(stock, filters.category)) filters.category = "";
  filters.focused = String(stock.id);
  filters.view = "focus";
  render();
  byId("focused-stock").focus();
}

function relatedCompanies(stock) {
  const section = element("section", "related-companies");
  const heading = element("h2", "", "相關公司");
  heading.id = "related-companies-title";
  section.setAttribute("aria-labelledby", heading.id);
  section.append(heading);

  if (!stock.related?.length) {
    section.append(element("p", "related-empty", "目前沒有相關公司資料"));
    return section;
  }

  const list = element("ul", "related-list");
  for (const company of stock.related) {
    const { market, symbol, name, relationship } = company;
    const tracked = state.stocks.find((item) => item.market === market && item.symbol === symbol);
    const entry = element("li", "related-company");
    const details = element("div", "related-details");
    details.append(
      element("p", "related-name", `${symbol} ${name}`),
      element("p", "related-meta", `${marketNames[market]} · ${relationship}`),
    );
    const action = button("button button-quiet", tracked ? "查看" : "加入自選股", async () => {
      if (tracked) {
        focusStock(tracked);
        return;
      }
      action.disabled = true;
      const saved = await write({ action: "add_stock", market, symbol, category_id: stock.category_id },
        byId("page-error"), `已加入 ${symbol} ${name}`);
      if (!saved) action.disabled = false;
    });
    action.dataset.relatedKey = `${stock.id}:${market}:${symbol}`;
    action.setAttribute("aria-label", tracked ? `查看 ${symbol} ${name}` : `將 ${symbol} ${name}加入自選股`);
    entry.append(details, action);
    list.append(entry);
  }
  section.append(list);
  return section;
}

function latestSession(stock, latest) {
  if (!latest) return element("p", "session-empty", "等待盤後行情");

  const [date, close] = latest;
  const isToday = date === quoteDates[stock.market === "US" ? 1 : 0];
  const summary = element("section", "session-summary");
  const heading = element("div", "session-heading", isToday ? "今日交易日" : "最新交易日");
  const tradingDay = element("time", "quote-date", date);
  tradingDay.dateTime = date;
  heading.append(tradingDay);

  const quote = element("div", "quote");
  const previousClose = stock.history.at(-2)?.[1];
  const change = previousClose > 0 ? close - previousClose : null;
  const quoteChange = element("span", "quote-change", change === null
    ? "漲跌 —" : `${changeFormat.format(change)} (${changeFormat.format(change / previousClose * 100)}%)`);
  quoteChange.title = "相較前一交易日收盤";
  if (change) {
    const useRed = stock.market === "US" ? change < 0 : change > 0;
    quoteChange.classList.add(useRed ? "market-red" : "market-green");
  }
  quote.append(element("span", "currency", "收盤"),
    element("span", "price", priceFormat.format(close)),
    element("span", "currency", stock.market === "US" ? "USD" : "TWD"), quoteChange);

  const lastCandle = stock.candles?.at(-1);
  const candle = lastCandle?.[0] === date ? lastCandle : [];
  const details = element("dl", "session-metrics");
  for (const [key, label, value] of [
    ["open", "開盤", candle[1]], ["high", "最高", candle[2]], ["low", "最低", candle[3]], ["volume", "成交量", candle[5]],
  ]) {
    const item = element("div", `session-${key}`);
    let display = "—";
    if (Number.isFinite(value)) {
      display = key === "volume" ? `${volumeFormat.format(value)} 股` : priceFormat.format(value);
    }
    item.append(element("dt", "", label), element("dd", "", display));
    details.append(item);
  }
  summary.append(heading, quote, details);
  return summary;
}

function stockCard(stock) {
  const card = element("article", "stock-card");
  const header = element("div", "card-head");
  const identity = element("div", "ticker-and-market");
  identity.append(element("h2", "ticker", stock.symbol), element("span", "market-tag", marketNames[stock.market]));
  header.append(identity);
  if (filters.view !== "focus") {
    header.append(button("button button-quiet", "單股", () => focusStock(stock)));
  }
  card.append(header, element("p", "stock-name", stock.name));
  const latest = stock.history.at(-1);
  if (filters.view === "focus") {
    card.append(StockTechnical.create(stock, state.settings.months));
    if (latest) {
      const first = stock.history[0][0];
      const dateRange = first === latest[0] ? first : `${first} ～ ${latest[0]}`;
      card.append(element("p", "history-coverage", `已載入 ${stock.history.length} 個交易日 · ${dateRange}`));
    }
  } else {
    card.append(latestSession(stock, latest));
  }
  if (stock.status) card.append(element("p", "card-status", stock.status));
  const actions = element("div", "card-actions");
  const category = element("select");
  category.setAttribute("aria-label", `${stock.symbol} 的分類`);
  setOptions(category, categoryOptions(), stock.category_id ?? "");
  category.addEventListener("change", async () => {
    category.disabled = true;
    if (!await write({ action: "move_stock", id: stock.id, category_id: Number(category.value) || null },
      byId("page-error"), "已移動股票")) {
      category.value = stock.category_id ?? "";
      category.disabled = false;
    }
  });
  const remove = button("button button-danger", "移除", async () => {
    if (!window.confirm(`從自選清單移除 ${stock.symbol} ${stock.name}？`)) return;
    remove.disabled = true;
    if (!await write({ action: "remove_stock", id: stock.id }, byId("page-error"), "已移除股票")) {
      remove.disabled = false;
    }
  });
  remove.setAttribute("aria-label", `移除 ${stock.symbol}`);
  actions.append(category, remove);
  card.append(actions);
  return card;
}

const chartObserver = new ResizeObserver((entries) => {
  for (const { target } of entries) StockTechnical.redraw(target);
});

const stockDialog = byId("stock-dialog");
const search = byId("stock-search");
const results = byId("search-results");
const searchStatus = byId("search-status");
const submitStock = byId("submit-stock");
let timer;
let searchController;
let selectedStock;
let matches = [];
let activeResult = -1;

function showMatches(items) {
  matches = items;
  activeResult = -1;
  search.removeAttribute("aria-activedescendant");
  results.hidden = !items.length;
  search.setAttribute("aria-expanded", String(Boolean(items.length)));
  results.replaceChildren(...items.map((stock, index) => {
    const item = element("li");
    item.id = `search-item-${index}`;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", "false");
    item.append(element("strong", "", stock.symbol), element("span", "", `${stock.name} · ${marketNames[stock.market]}`));
    item.addEventListener("click", () => selectStock(stock));
    return item;
  }));
}

function cancelSearch() {
  clearTimeout(timer);
  searchController?.abort();
  searchController = null;
  showMatches([]);
}

function selectStock(stock) {
  cancelSearch();
  selectedStock = stock;
  search.value = `${stock.symbol} ${stock.name}`;
  searchStatus.textContent = `已選擇 ${stock.symbol} · ${marketNames[stock.market]}`;
  submitStock.disabled = false;
  search.focus();
}

function openStockDialog() {
  if (!state) return;
  cancelSearch();
  selectedStock = null;
  search.value = "";
  byId("search-market").value = filters.market;
  setOptions(byId("stock-category"), categoryOptions(), filters.category === "none" ? "" : filters.category);
  submitStock.disabled = true;
  searchStatus.textContent = "輸入代碼或公司名稱來搜尋";
  showError(stockDialog.querySelector(".form-error"), null);
  stockDialog.showModal();
  search.focus();
}

function queueSearch() {
  cancelSearch();
  selectedStock = null;
  submitStock.disabled = true;
  const query = search.value.trim();
  if (!query) {
    searchStatus.textContent = "輸入代碼或公司名稱來搜尋";
    return;
  }
  searchStatus.textContent = "搜尋中…";
  timer = setTimeout(async () => {
    const controller = new AbortController();
    searchController = controller;
    try {
      const params = new URLSearchParams({ q: query, market: byId("search-market").value });
      const { results: items } = await request(`/api/search?${params}`, { signal: controller.signal });
      if (controller.signal.aborted || !stockDialog.open) return;
      searchStatus.textContent = items.length ? "選擇搜尋結果來加入清單" : "找不到符合的股票";
      showMatches(items);
    } catch (error) {
      if (!controller.signal.aborted) searchStatus.textContent = error.message || "搜尋失敗";
    }
  }, 220);
}

search.addEventListener("input", (event) => {
  if (!event.isComposing) queueSearch();
});
search.addEventListener("compositionend", queueSearch);
byId("search-market").addEventListener("change", () => {
  if (selectedStock) search.value = selectedStock.symbol;
  queueSearch();
});
search.addEventListener("keydown", (event) => {
  if (event.isComposing || !matches.length) return;
  if (event.key === "Escape") {
    event.preventDefault();
    showMatches([]);
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (activeResult !== -1) selectStock(matches[activeResult]);
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (activeResult !== -1) results.children[activeResult].setAttribute("aria-selected", "false");
    const step = event.key === "ArrowDown" ? 1 : -1;
    activeResult = activeResult === -1 ? (step === 1 ? 0 : matches.length - 1) :
      (activeResult + step + matches.length) % matches.length;
    const option = results.children[activeResult];
    option.setAttribute("aria-selected", "true");
    search.setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({ block: "nearest" });
  }
});
stockDialog.addEventListener("close", cancelSearch);
byId("stock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedStock || submitStock.disabled) return;
  submitStock.disabled = true;
  const { market, symbol } = selectedStock;
  const category = byId("stock-category").value;
  const saved = await write({ action: "add_stock", market, symbol, category_id: Number(category) || null },
    stockDialog.querySelector(".form-error"), "已加入自選股");
  if (saved) {
    if (filters.market && filters.market !== market) filters.market = "";
    if (filters.category && filters.category !== (category || "none")) filters.category = "";
    if (state) {
      const stock = state.stocks.find((item) => item.market === market && item.symbol === symbol);
      if (stock) {
        filters.focused = String(stock.id);
        const visible = state.stocks.filter((item) => categoryMatch(item, filters.category) &&
          (!filters.market || item.market === filters.market));
        filters.page = Math.floor(visible.indexOf(stock) / (state.settings.rows * state.settings.columns));
      }
      render();
    }
    stockDialog.close();
  } else {
    submitStock.disabled = !selectedStock;
  }
});

const categoryDialog = byId("category-dialog");
const categoryInput = byId("new-category");
const categoryError = categoryDialog.querySelector(".form-error");

function renderCategoryDialog() {
  const rows = state.categories.map(({ id, name }) => {
    const row = element("div", "category-row");
    const remove = button("button button-danger", "刪除", async () => {
      if (!window.confirm(`刪除「${name}」？此分類的股票會移到未分類。`)) return;
      remove.disabled = true;
      if (await write({ action: "remove_category", id }, categoryError, "已刪除分類")) {
        renderCategoryDialog();
        categoryInput.focus();
      } else {
        remove.disabled = false;
      }
    });
    remove.setAttribute("aria-label", `刪除分類 ${name}`);
    row.append(element("span", "", name), remove);
    return row;
  });
  byId("manage-categories").replaceChildren(...rows);
}

byId("open-category").addEventListener("click", () => {
  if (!state) return;
  categoryInput.value = "";
  showError(categoryError, null);
  renderCategoryDialog();
  categoryDialog.showModal();
  categoryInput.focus();
});
byId("category-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = categoryInput.value.trim();
  if (!name || event.submitter?.disabled) return;
  const submit = event.currentTarget.querySelector("[type=submit]");
  submit.disabled = true;
  if (await write({ action: "add_category", name }, categoryError, "已新增分類")) {
    categoryInput.value = "";
    renderCategoryDialog();
    categoryInput.focus();
  }
  submit.disabled = false;
});

const settingsDialog = byId("settings-dialog");
const settingsError = settingsDialog.querySelector(".form-error");
const sizes = Array.from({ length: 6 }, (_, i) => ({ value: i + 1, label: i + 1 }));
function openSettings() {
  if (!state) return;
  const { rows, columns, months, enabled } = state.settings;
  setOptions(byId("layout-rows"), sizes, rows);
  setOptions(byId("layout-columns"), sizes, columns);
  byId("history-months").value = months;
  byId("background-fetch").checked = enabled;
  showError(settingsError, null);
  settingsDialog.showModal();
}
byId("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("[type=submit]");
  if (submit.disabled) return;
  submit.disabled = true;
  const settings = {
    action: "settings",
    rows: Number(byId("layout-rows").value),
    columns: Number(byId("layout-columns").value),
    months: Number(byId("history-months").value),
    enabled: byId("background-fetch").checked,
  };
  filters.page = 0;
  if (await write(settings, settingsError, "已儲存設定")) settingsDialog.close();
  submit.disabled = false;
});

for (const closeButton of document.querySelectorAll("[data-close]")) {
  closeButton.addEventListener("click", () => closeButton.closest("dialog").close());
}
for (const [view, id] of [["grid", "grid-view"], ["focus", "focus-view"]]) {
  byId(id).addEventListener("click", () => {
    if (!state) return;
    filters.view = view;
    render();
  });
}
for (const [offset, id] of [[-1, "previous"], [1, "next"]]) {
  byId(id).addEventListener("click", () => {
    filters.page += offset;
    render();
    if (byId(id).disabled) byId(offset === 1 ? "previous" : "next").focus();
  });
}
byId("focused-stock").addEventListener("change", (event) => {
  filters.focused = event.target.value;
  render();
});
byId("open-stock").addEventListener("click", openStockDialog);
byId("open-settings").addEventListener("click", openSettings);
byId("open-layout").addEventListener("click", openSettings);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void loadState({ quiet: true });
});
void loadState();
window.setInterval(() => void loadState({ quiet: true }), 20000);
