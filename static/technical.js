"use strict";

globalThis.StockTechnical = (() => {
  const charts = new WeakMap();
  const intervals = [["daily", "日線"], ["weekly", "週線"], ["monthly", "月線"]];
  const ranges = [[1, "1 個月"], [3, "3 個月"], [6, "6 個月"], [12, "12 個月"], [24, "24 個月"], [0, "全部"]];
  const averages = [
    [5, "#7451a5"], [10, "#af6114"], [20, "#235da3"], [60, "#087454"],
    [120, "#ab3864"], [240, "#596d27"],
  ];
  const colors = {
    close: "#174d43", grid: "#e3ebe7", axis: "#566c63", cursor: "#6f827a",
    k: "#235da3", d: "#af6114", dif: "#235da3", signal: "#ab3864",
    taiwanGain: "#b33d43", taiwanLoss: "#087454",
  };
  const number = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 });
  const percent = new Intl.NumberFormat("zh-TW", {
    maximumFractionDigits: 2, minimumFractionDigits: 2, signDisplay: "exceptZero",
  });
  const preferences = {
    period: "daily", months: null, dates: null, dateDraft: null, dateOpen: false,
    averagePeriods: new Set([5, 10, 20, 60]),
  };
  const day = (value) => new Date(`${value}T00:00:00Z`);

  function cutoff(latest, months) {
    const timestamp = day(latest);
    const preferredDay = timestamp.getUTCDate();
    timestamp.setUTCDate(1);
    timestamp.setUTCMonth(timestamp.getUTCMonth() - months);
    const lastOfMonth = new Date(Date.UTC(timestamp.getUTCFullYear(), timestamp.getUTCMonth() + 1, 0));
    timestamp.setUTCDate(Math.min(preferredDay, lastOfMonth.getUTCDate()));
    return timestamp.toISOString().slice(0, 10);
  }

  function when(value, formatter = priceFormat) {
    return Number.isFinite(value) ? formatter.format(value) : "—";
  }

  function chartControl(label, key, action) {
    const control = button("technical-control", label, action);
    control.dataset.technicalKey = key;
    return control;
  }

  function makeChartSection(title, empty, createCanvas) {
    const section = element("section", "technical-panel");
    const heading = element("div", "technical-panel-heading");
    heading.append(element("h3", "", title));
    const legend = element("div", "technical-legend");
    heading.append(legend);
    const blank = element("p", "technical-chart-empty", empty);
    const canvas = createCanvas(title);
    section.append(heading, canvas, blank);
    return { section, legend, canvas, blank };
  }

  function create(stock, maxMonths) {
    const section = element("section", "technical-view");
    section.setAttribute("aria-label", "技術分析");
    const title = element("h3", "technical-title", "技術分析");
    const toolbar = element("div", "technical-toolbar");
    const intervalGroup = element("div", "technical-control-group");
    const rangeGroup = element("div", "technical-control-group");
    intervalGroup.setAttribute("aria-label", "K 線週期");
    rangeGroup.setAttribute("aria-label", "圖表顯示範圍");
    intervalGroup.setAttribute("role", "group");
    rangeGroup.setAttribute("role", "group");
    toolbar.append(intervalGroup, rangeGroup);

    const metrics = element("dl", "technical-metrics");
    const fields = {};
    for (const [name, label] of [
      ["date", "交易日期"], ["close", "收盤"], ["change", "漲跌"], ["open", "開盤"],
      ["high", "最高"], ["low", "最低"], ["volume", "成交量"],
    ]) {
      const item = element("div", `technical-metric technical-metric-${name}`);
      const value = element("dd", "", "—");
      fields[name] = value;
      item.append(element("dt", "", label), value);
      metrics.append(item);
    }
    const graphs = element("div", "technical-graphs");
    const help = element("p", "technical-help");
    const live = element("p", "sr-only");
    live.setAttribute("role", "status");

    let bars = [], previous = null, index = -1, queued = false, dateAxis = "price";
    const panels = {};
    const plot = (kind) => (label) => {
      const canvas = element("canvas", `technical-plot technical-plot-${kind}`);
      canvas.setAttribute("aria-label", `${stock.symbol} ${label}。使用左右方向鍵查看各交易期間。`);
      canvas.setAttribute("role", "img");
      canvas.dataset.technicalKey = `chart-${kind}`;
      canvas.tabIndex = 0;
      canvas.addEventListener("pointermove", (event) => {
        if (event.pointerType !== "touch") selectAt(event, canvas);
      });
      canvas.addEventListener("pointerleave", (event) => {
        if (event.pointerType !== "touch" && document.activeElement !== canvas) select(bars.length - 1);
      });
      canvas.addEventListener("click", (event) => selectAt(event, canvas));
      canvas.addEventListener("keydown", (event) => {
        if (!bars.length) return;
        const positions = {
          ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: bars.length - 1,
        };
        if (!Object.hasOwn(positions, event.key)) return;
        event.preventDefault();
        select(positions[event.key]);
        live.textContent = `${fields.date.textContent} 收盤 ${fields.close.textContent} 成交量 ${fields.volume.textContent}`;
      });
      charts.set(canvas, () => ({
        bars, kind, index, market: stock.market, averages: preferences.averagePeriods,
        showDates: kind === dateAxis,
      }));
      return canvas;
    };

    for (const [kind, label, empty] of [
      ["price", "K 線與均線", "此期間沒有收盤價"],
      ["volume", "成交量", "此期間沒有成交量資料"],
      ["kd", "KD", "至少需要 9 根連續且有高低價的 K 線"],
      ["macd", "MACD", "至少需要 26 根收盤價計算 DIF，34 根才有完整 MACD"],
    ]) {
      panels[kind] = makeChartSection(label, empty, plot(kind));
      graphs.append(panels[kind].section);
    }

    const averageControls = averages.map(([period, color]) => {
      const control = chartControl(`MA${period}`, `ma-${period}`, () => {
        if (!preferences.averagePeriods.delete(period)) preferences.averagePeriods.add(period);
        renderSelection();
        redrawPanels();
      });
      control.style.setProperty("--series-color", color);
      control.classList.add("technical-average");
      return [period, control];
    });
    panels.price.legend.append(...averageControls.map(([, control]) => control));
    const legendValues = {};
    function addLegend(kind, label, color) {
      const item = element("span", "technical-legend-value");
      if (color) item.style.setProperty("--series-color", color);
      panels[kind].legend.append(item);
      legendValues[label] = item;
    }
    addLegend("volume", "成交量");
    addLegend("kd", "K", colors.k);
    addLegend("kd", "D", colors.d);
    addLegend("macd", "DIF", colors.dif);
    addLegend("macd", "訊號", colors.signal);
    addLegend("macd", "柱狀");

    const intervalControls = intervals.map(([key, label]) => {
      const control = chartControl(label, `period-${key}`, () => {
        preferences.period = key;
        updateSeries();
      });
      return [key, control];
    });
    const canDisplayRange = (months) => months === 0 || maxMonths === 0 || months < maxMonths;
    const rangeControls = ranges.filter(([months]) => canDisplayRange(months)).map(([months, label]) => {
      const control = chartControl(label, `range-${months}`, () => {
        preferences.months = months;
        preferences.dates = null;
        preferences.dateDraft = null;
        preferences.dateOpen = false;
        updateSeries();
      });
      return [months, control];
    });
    const dateForm = element("form", "technical-date-form");
    dateForm.id = "technical-date-form";
    const dateError = element("p", "technical-date-error");
    dateError.setAttribute("role", "alert");
    dateError.hidden = true;
    const dateFields = {};
    for (const [key, label] of [["start", "起始日期"], ["end", "結束日期"]]) {
      const field = element("label", "technical-date-field", label);
      const input = element("input");
      input.type = "date";
      input.required = true;
      input.min = "0001-01-01";
      input.max = "9999-12-31";
      input.name = key;
      input.dataset.technicalKey = `date-${key}`;
      input.addEventListener("input", () => {
        preferences.dateDraft = { start: dateFields.start.value, end: dateFields.end.value };
        dateError.hidden = true;
      });
      dateFields[key] = input;
      field.append(input);
      dateForm.append(field);
    }
    const applyDates = element("button", "button button-primary", "套用日期");
    applyDates.type = "submit";
    applyDates.dataset.technicalKey = "date-apply";
    dateForm.append(applyDates, dateError);
    dateForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const start = dateFields.start.value, end = dateFields.end.value;
      if (start > end) {
        dateError.textContent = "起始日期須早於或等於結束日期。";
        dateError.hidden = false;
        return;
      }
      dateError.hidden = true;
      preferences.dates = { start, end };
      preferences.dateDraft = preferences.dates;
      updateSeries();
    });
    const dateControl = chartControl("自訂日期", "range-custom", () => {
      preferences.dateOpen = !preferences.dateOpen;
      renderDateForm();
      if (preferences.dateOpen) dateFields.start.focus();
    });
    dateControl.setAttribute("aria-controls", dateForm.id);
    intervalGroup.append(...intervalControls.map(([, control]) => control));
    rangeGroup.append(...rangeControls.map(([, control]) => control), dateControl);
    section.append(title, toolbar, dateForm, metrics, graphs, help, live);

    function selectAt(event, canvas) {
      const rect = canvas.getBoundingClientRect();
      const { left, right } = plotBounds(rect.width, rect.height);
      const position = (event.clientX - rect.left - left) / (right - left);
      select(Math.floor(position * bars.length));
    }

    function select(nextIndex) {
      const next = Math.max(0, Math.min(bars.length - 1, nextIndex));
      if (index === next || !bars.length) return;
      index = next;
      renderSelection();
      redrawPanels();
    }

    function updateSeries() {
      const dates = preferences.dates;
      const calculated = StockIndicators.buildSeries(stock.history, stock.candles ?? [], preferences.period, dates);
      const preferred = preferences.months ?? (maxMonths === 0 ? 12 : 0);
      const range = canDisplayRange(preferred) ? preferred : 0;
      bars = calculated;
      previous = null;
      if (dates || (range && calculated.length)) {
        const firstDate = dates ? dates.start : cutoff(calculated.at(-1).date, range);
        const first = calculated.findIndex((bar) => bar.date >= firstDate);
        bars = first < 0 ? [] : calculated.slice(first);
        previous = first > 0 ? calculated[first - 1] : null;
      }
      index = bars.length - 1;
      const available = {
        price: bars.length > 0,
        volume: bars.some((bar) => Number.isFinite(bar.volume)),
        kd: bars.some((bar) => Number.isFinite(bar.k) && Number.isFinite(bar.d)),
        macd: bars.some((bar) => Number.isFinite(bar.dif)),
      };
      dateAxis = Object.keys(available).findLast((kind) => available[kind]);
      for (const [kind, panel] of Object.entries(panels)) {
        panel.canvas.hidden = !available[kind];
        panel.blank.hidden = available[kind];
      }
      help.textContent = bars.some((bar) => !Number.isFinite(bar.open))
        ? "缺少開高低價的區段以收盤線顯示。點按圖表或使用方向鍵查看數值。"
        : "點按圖表或使用方向鍵查看數值。";
      help.hidden = !bars.length;
      for (const [key, control] of intervalControls) {
        control.setAttribute("aria-pressed", String(preferences.period === key));
      }
      for (const [months, control] of rangeControls) {
        control.setAttribute("aria-pressed", String(!dates && range === months));
      }
      renderDateForm();
      renderSelection();
      redrawPanels();
    }

    function renderDateForm() {
      dateForm.hidden = !preferences.dateOpen;
      dateControl.setAttribute("aria-expanded", String(preferences.dateOpen));
      dateControl.setAttribute("aria-pressed", String(Boolean(preferences.dates)));
      if (!preferences.dateOpen) return;
      const dates = preferences.dateDraft ?? {
        start: bars[0]?.startDate ?? "",
        end: bars.at(-1)?.date ?? "",
      };
      dateFields.start.value = dates.start;
      dateFields.end.value = dates.end;
    }

    function renderSelection() {
      const selected = bars[index];
      const prior = bars[index - 1] ?? previous;
      const change = selected && prior?.close ? selected.close - prior.close : null;
      fields.date.textContent = selected
        ? (selected.startDate === selected.date ? selected.date : `${selected.startDate} ～ ${selected.date}`)
        : "—";
      for (const field of ["close", "open", "high", "low"]) {
        fields[field].textContent = when(selected?.[field]);
      }
      if (selected) fields.close.textContent += stock.market === "US" ? " USD" : " TWD";
      fields.change.textContent = change === null
        ? "—" : `${percent.format(change)} (${percent.format(change / prior.close * 100)}%)`;
      if (change !== null) fields.change.style.color = riseColor(stock.market, change);
      else fields.change.style.removeProperty("color");
      fields.volume.textContent = Number.isFinite(selected?.volume) ? `${number.format(selected.volume)} 股` : "—";
      for (const [period, control] of averageControls) {
        control.textContent = `MA${period} ${when(selected?.ma[period])}`;
        control.setAttribute("aria-pressed", String(preferences.averagePeriods.has(period)));
      }
      for (const [label, field] of [["K", "k"], ["D", "d"], ["DIF", "dif"], ["訊號", "signal"], ["柱狀", "histogram"]]) {
        legendValues[label].textContent = `${label} ${when(selected?.[field])}`;
      }
      legendValues["成交量"].textContent = fields.volume.textContent;
    }

    function redrawPanels() {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        if (!section.isConnected) return;
        for (const panel of Object.values(panels)) redraw(panel.canvas);
      });
    }

    updateSeries();
    return section;
  }

  function riseColor(market, change) {
    if (!change) return colors.axis;
    return (market === "US" ? change < 0 : change > 0) ? colors.taiwanGain : colors.taiwanLoss;
  }

  function plotBounds(width, height, showDates = false) {
    return { left: 4, right: width - (width < 380 ? 48 : 65),
      top: 9, bottom: height - (showDates ? 21 : 9) };
  }

  function domain(values, padding, includeZero = false) {
    const list = values.filter(Number.isFinite);
    if (includeZero) list.push(0);
    const min = Math.min(...list), max = Math.max(...list);
    const gap = Math.max((max - min) * padding, Math.abs(max) * .002, .01);
    return [min - gap, max + gap];
  }

  function redraw(canvas) {
    const state = charts.get(canvas)?.();
    const { width, height } = canvas.getBoundingClientRect();
    if (!state?.bars.length || width < 60 || height < 40) return;
    const { bars, kind, index, market, averages: active, showDates } = state;
    const density = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(width * density), pixelHeight = Math.round(height * density);
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(density, 0, 0, density, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "10px system-ui, sans-serif";
    ctx.lineJoin = "round";

    const { left, right, top, bottom } = plotBounds(width, height, showDates);
    const gap = (right - left) / bars.length;
    const x = (i) => left + gap * (i + .5);
    const candleWidth = Math.min(15, Math.max(1, gap * .68));
    let low, high;
    if (kind === "price") {
      const prices = bars.flatMap((bar) => [
        bar.close, bar.high, bar.low, ...[...active].map((period) => bar.ma[period]),
      ]);
      [low, high] = domain(prices, .08);
    } else if (kind === "volume") {
      low = 0;
      high = Math.max(1, ...bars.map((bar) => bar.volume ?? 0)) * 1.08;
    } else if (kind === "kd") {
      [low, high] = [0, 100];
    } else {
      [low, high] = domain(bars.flatMap((bar) => [bar.dif, bar.signal, bar.histogram]), .1, true);
    }
    const y = (v) => bottom - (v - low) / (high - low) * (bottom - top);

    function rule(x1, y1, x2, y2, color) {
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = color;
      ctx.stroke();
    }

    const axisCount = kind === "price" ? 5 : 3;
    const axisValues = kind === "kd" ? [0, 20, 50, 80, 100]
      : Array.from({ length: axisCount }, (_, i) => low + (high - low) * i / (axisCount - 1));
    for (const value of axisValues) {
      const label = kind === "volume" && value >= 10_000
        ? `${number.format(value / 10_000)} 萬`
        : kind === "kd" || kind === "volume" ? number.format(value) : priceFormat.format(value);
      rule(left, y(value), right, y(value), colors.grid);
      ctx.fillStyle = colors.axis;
      ctx.fillText(label, right + 5, y(value) + 3);
    }
    const timeTicks = Math.min(bars.length, Math.max(2, Math.floor((right - left) / 105)));
    const showYears = day(bars.at(-1).date) - day(bars[0].date) > 366 * 86400000;
    ctx.fillStyle = colors.axis;
    for (let tick = 0; tick < timeTicks; tick += 1) {
      const i = timeTicks === 1 ? 0 : Math.round(tick * (bars.length - 1) / (timeTicks - 1));
      const label = (showYears ? bars[i].date.slice(0, 7) : bars[i].date.slice(5)).replace("-", "/");
      const labelWidth = ctx.measureText(label).width;
      if (showDates) {
        ctx.fillText(label, Math.max(left, Math.min(right - labelWidth, x(i) - labelWidth / 2)), height - 5);
      }
      rule(x(i), top, x(i), bottom, colors.grid);
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top - 1, right - left, bottom - top + 2);
    ctx.clip();

    function line(getValue, color, dashed = false) {
      ctx.beginPath();
      let ongoing = false;
      bars.forEach((bar, i) => {
        const value = getValue(bar, i);
        if (!Number.isFinite(value)) {
          ongoing = false;
        } else if (!ongoing) {
          ctx.moveTo(x(i), y(value));
          ongoing = true;
        } else {
          ctx.lineTo(x(i), y(value));
        }
      });
      if (dashed) ctx.setLineDash([3, 2]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.3;
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
    }

    function marker(i, value, color, radius) {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x(i), y(value), radius, 0, Math.PI * 2);
      ctx.fill();
    }

    if (kind === "price") {
      const hasCandle = (bar) => Number.isFinite(bar?.open);
      const hasMissing = bars.some((bar) => !hasCandle(bar));
      if (hasMissing) {
        line((bar, i) => !hasCandle(bar) || ((i > 0 && !hasCandle(bars[i - 1]))
          || (i < bars.length - 1 && !hasCandle(bars[i + 1]))) ? bar.close : null, colors.close, bars.some(hasCandle));
      }
      bars.forEach((bar, i) => {
        if (!hasCandle(bar)) {
          if (bars.length === 1) marker(i, bar.close, colors.close, 2.5);
          return;
        }
        const color = riseColor(market, bar.close - bar.open);
        rule(x(i), y(bar.high), x(i), y(bar.low), color);
        ctx.fillStyle = color;
        ctx.fillRect(x(i) - candleWidth / 2, Math.min(y(bar.close), y(bar.open)), candleWidth,
          Math.max(1, Math.abs(y(bar.close) - y(bar.open))));
      });
      for (const [period, color] of averages) {
        if (active.has(period)) line((bar) => bar.ma[period], color);
      }
    } else if (kind === "volume") {
      bars.forEach((bar, i) => {
        if (!Number.isFinite(bar.volume)) return;
        ctx.fillStyle = Number.isFinite(bar.open) ? riseColor(market, bar.close - bar.open) : colors.axis;
        ctx.globalAlpha = .72;
        ctx.fillRect(x(i) - candleWidth / 2, y(bar.volume), candleWidth, bottom - y(bar.volume));
      });
      ctx.globalAlpha = 1;
    } else if (kind === "kd") {
      line((bar) => bar.k, colors.k);
      line((bar) => bar.d, colors.d);
    } else {
      rule(left, y(0), right, y(0), "#b9c8c0");
      bars.forEach((bar, i) => {
        if (!Number.isFinite(bar.histogram)) return;
        ctx.fillStyle = riseColor(market, bar.histogram);
        ctx.globalAlpha = .65;
        ctx.fillRect(x(i) - candleWidth / 2, Math.min(y(0), y(bar.histogram)), candleWidth,
          Math.abs(y(0) - y(bar.histogram)));
      });
      ctx.globalAlpha = 1;
      line((bar) => bar.dif, colors.dif);
      line((bar) => bar.signal, colors.signal);
    }

    if (index >= 0) {
      ctx.setLineDash([4, 4]);
      rule(x(index), top, x(index), bottom, colors.cursor);
      ctx.setLineDash([]);
      const activeBar = bars[index];
      const value = { price: activeBar.close, volume: activeBar.volume, kd: activeBar.k, macd: activeBar.dif }[kind];
      if (Number.isFinite(value)) marker(index, value, colors.cursor, 2.4);
    }
    ctx.restore();
  }

  return { create, redraw };
})();
