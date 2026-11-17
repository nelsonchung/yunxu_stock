"use strict";

const StockIntraday = (() => {
  const chartData = new WeakMap();
  const timeFormat = new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const percentFormat = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 1 });

  function format(value, formatter = priceFormat) {
    return Number.isFinite(value) ? formatter.format(value) : "—";
  }

  function lots(value) {
    return Number.isFinite(value) ? `${volumeFormat.format(value)} 張` : "—";
  }

  function numberPair(name, value) {
    const field = element("div", "intraday-stat");
    field.append(element("dt", "", name), element("dd", "", value));
    return field;
  }

  function depthTable(quote, expanded) {
    const bids = quote.bids ?? [];
    const asks = quote.asks ?? [];
    if (!bids.length && !asks.length) return null;
    const detail = element("details", "intraday-depth");
    detail.open = expanded;
    detail.append(element("summary", "", "最佳五檔"));
    const table = element("table");
    const header = table.createTHead().insertRow();
    for (const name of ["買量", "委買價", "委賣價", "賣量"]) {
      const cell = element("th", "", name);
      cell.scope = "col";
      header.append(cell);
    }
    const body = table.createTBody();
    const levels = [...bids, ...asks];
    const scale = Math.max(1, ...levels.map(([, size]) => size || 0));
    for (let i = 0; i < Math.max(bids.length, asks.length); i += 1) {
      const row = body.insertRow();
      const [bidPrice, bidSize] = bids[i] ?? [];
      const [askPrice, askSize] = asks[i] ?? [];
      for (const [side, value] of [["bid", bidSize], ["price", bidPrice], ["price", askPrice], ["ask", askSize]]) {
        const isPrice = side === "price";
        const label = isPrice && value === 0 ? "市價" : format(value, isPrice ? priceFormat : volumeFormat);
        const cell = element("td", isPrice ? "" : `intraday-depth-${side}`, label);
        if (!isPrice && Number.isFinite(value)) {
          cell.style.setProperty("--depth", `${value / scale * 100}%`);
        }
        row.append(cell);
      }
    }
    detail.append(table);
    return detail;
  }

  function tradeBalance({ bid_volume: bid, ask_volume: ask }) {
    if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid + ask <= 0) return null;
    const block = element("div", "intraday-balance");
    const labels = element("div", "intraday-balance-labels");
    const share = bid / (bid + ask) * 100;
    labels.append(
      element("span", "market-green", `內盤 ${percentFormat.format(share)}%`),
      element("span", "market-red", `外盤 ${percentFormat.format(100 - share)}%`),
    );
    const bar = element("div", "intraday-balance-bar");
    bar.setAttribute("aria-hidden", "true");
    bar.style.setProperty("--inside", `${share}%`);
    block.append(labels, bar);
    return block;
  }

  function validPoints(quote) {
    const start = Date.parse(`${quote.date}T09:00:00+08:00`);
    const end = Date.parse(`${quote.date}T13:30:00+08:00`);
    return (quote.points ?? []).map(([time, close, volume]) => [Date.parse(time), close, volume])
      .filter(([time, close]) => time >= start && time <= end && Number.isFinite(close))
      .sort(([a], [b]) => a - b);
  }

  function create(stock, quote, session, depthOpen = true) {
    const summary = element("section", "session-summary intraday-summary");
    const age = Date.now() / 1000 - quote.fetched_at;
    const label = !session || quote.closed ? "盤中整股快照" :
      age > 90 ? "報價暫未更新" : "盤中整股";
    const timestamp = new Date(quote.timestamp);
    const header = element("div", "session-heading", label);
    const time = element("time", "quote-date",
      Number.isFinite(timestamp.getTime()) ? `${quote.date} ${timeFormat.format(timestamp)}` : quote.date);
    if (Number.isFinite(timestamp.getTime())) time.dateTime = timestamp.toISOString();
    header.append(time);

    const headline = element("div", "quote");
    const direction = quote.change > 0 ? " market-red" : quote.change < 0 ? " market-green" : "";
    const change = Number.isFinite(quote.change) ? format(quote.change, changeFormat) : "漲跌 —";
    const rate = Number.isFinite(quote.change_percent) ? ` (${format(quote.change_percent, changeFormat)}%)` : "";
    headline.append(element("span", "currency", "成交"), element("span", "price", format(quote.price)),
      element("span", `quote-change${direction}`, change + rate));

    const metrics = element("dl", "intraday-metrics");
    for (const [name, value] of [
      ["昨收", format(quote.previous_close)], ["開盤", format(quote.open)],
      ["最高", format(quote.high)], ["最低", format(quote.low)],
      ["均價", format(quote.average)], ["成交量", lots(quote.volume)],
      ["成交額", Number.isFinite(quote.value) ? `${format(quote.value / 1e8)} 億` : "—"],
      ["振幅", Number.isFinite(quote.amplitude) ? `${format(quote.amplitude)}%` : "—"],
    ]) metrics.append(numberPair(name, value));

    summary.append(header, headline);
    const points = validPoints(quote);
    if (points.length) {
      const chart = element("canvas", "intraday-plot");
      chart.setAttribute("role", "img");
      chart.setAttribute("aria-label", `${stock.symbol} 盤中分時走勢與成交量，${points.length} 筆分線`);
      chartData.set(chart, { quote, points });
      summary.append(chart);
    } else {
      summary.append(element("p", "intraday-chart-empty", "等待分時行情"));
    }
    summary.append(metrics);
    const balance = tradeBalance(quote);
    const depth = depthTable(quote, depthOpen);
    if (balance) summary.append(balance);
    if (depth) summary.append(depth);
    return summary;
  }

  function redraw(canvas) {
    const data = chartData.get(canvas);
    const { width, height } = canvas.getBoundingClientRect();
    if (!data || width < 55 || height < 60) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const { quote, points } = data;
    const prices = points.map(([, close]) => close);
    if (Number.isFinite(quote.previous_close)) prices.push(quote.previous_close);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const pad = Math.max((max - min) * .1, max * .002, .01);
    const upper = max + pad;
    const lower = min - pad;
    const start = Date.parse(`${quote.date}T09:00:00+08:00`);
    const duration = 270 * 60_000;
    const right = width - (width < 220 ? 46 : 58);
    const bottom = height - 40;
    const x = (t) => 3 + (t - start) / duration * (right - 3);
    const y = (price) => bottom - (price - lower) / (upper - lower) * (bottom - 9);
    const baseline = Number.isFinite(quote.previous_close) ? y(quote.previous_close) : null;
    ctx.font = "10px system-ui, sans-serif";

    for (let i = 0; i < 4; i += 1) {
      const price = lower + (upper - lower) * i / 3;
      ctx.beginPath();
      ctx.moveTo(3, y(price));
      ctx.lineTo(right, y(price));
      ctx.strokeStyle = "#dce6e0";
      ctx.stroke();
      if (baseline === null || Math.abs(baseline - y(price)) > 12) {
        ctx.fillStyle = "#4b6158";
        ctx.fillText(format(price), right + 4, y(price) + 3, width - right - 4);
      }
    }
    if (baseline !== null) {
      ctx.beginPath();
      ctx.setLineDash([4, 3]);
      ctx.moveTo(3, baseline);
      ctx.lineTo(right, baseline);
      ctx.strokeStyle = "#5f7469";
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#48645a";
      ctx.fillRect(right + 2, baseline - 7, width - right - 2, 14);
      ctx.fillStyle = "#fff";
      ctx.fillText(format(quote.previous_close), right + 4, baseline + 3, width - right - 5);
    }
    const ticks = width < 220 ? [[0, "09:00"], [135, "11:15"], [270, "13:30"]] :
      [[0, "09:00"], [60, "10:00"], [120, "11:00"], [180, "12:00"], [270, "13:30"]];
    for (const [minute, label] of ticks) {
      const place = x(start + minute * 60_000);
      ctx.fillStyle = "#4b6158";
      ctx.fillText(label, Math.max(3, Math.min(place - 11, right - 26)), height - 4);
    }

    const maxVolume = Math.max(1, ...points.map(([, , volume]) => volume || 0));
    const barWidth = Math.max(.7, (right - 3) / 270 * .85);
    for (const [time, , volume] of points) {
      if (!(volume > 0)) continue;
      const barHeight = volume / maxVolume * 19;
      ctx.fillStyle = "#87a6ac";
      ctx.fillRect(x(time) - barWidth / 2, height - 17 - barHeight, barWidth, barHeight);
    }
    ctx.beginPath();
    points.forEach(([time, close], index) => {
      if (index === 0) ctx.moveTo(x(time), y(close));
      else ctx.lineTo(x(time), y(close));
    });
    ctx.strokeStyle = "#2e61a5";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    const [last, close] = points.at(-1);
    ctx.beginPath();
    ctx.arc(x(last), y(close), 2.5, 0, Math.PI * 2);
    ctx.fillStyle = "#244e87";
    ctx.fill();
  }

  return { create, redraw };
})();
