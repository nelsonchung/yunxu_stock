"use strict";

globalThis.StockIndicators = (() => {
  const MA_PERIODS = Object.freeze([5, 10, 20, 60, 120, 240]);
  const DAY_MS = 86_400_000;

  const positive = (value) => Number.isFinite(value) && value > 0;
  const volumeValue = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;

  function validDate(day) {
    if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
    const stamp = Date.parse(day);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === day;
  }

  function dailyBars(history, candles) {
    const bars = new Map();
    for (const [date, close] of history) {
      if (validDate(date) && positive(close)) {
        bars.set(date, { date, startDate: date, open: null, high: null, low: null, close, volume: null });
      }
    }
    for (const [date, open, high, low, close, volume] of candles) {
      if (!validDate(date) || !positive(close)) continue;
      const hasRange = positive(open) && positive(high) && positive(low) &&
        low <= Math.min(open, close) && high >= Math.max(open, close);
      bars.set(date, {
        date, startDate: date, close,
        open: hasRange ? open : null,
        high: hasRange ? high : null,
        low: hasRange ? low : null,
        volume: volumeValue(volume),
      });
    }
    return [...bars.values()].sort((left, right) => left.date.localeCompare(right.date));
  }

  function periodKey(day, period) {
    if (period === "monthly") return day.slice(0, 7);
    const date = new Date(day);
    const daysSinceMonday = (date.getUTCDay() + 6) % 7;
    return new Date(date.getTime() - daysSinceMonday * DAY_MS).toISOString().slice(0, 10);
  }

  function aggregate(bars, period) {
    if (period === "daily") return bars;
    const groups = new Map();
    for (const bar of bars) {
      const key = periodKey(bar.date, period);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(bar);
    }
    return [...groups.values()].map((group) => {
      const first = group[0];
      const last = group.at(-1);
      const hasRange = group.every(({ open }) => open !== null);
      const hasVolume = group.every(({ volume }) => volume !== null);
      const volume = hasVolume ? group.reduce((sum, bar) => sum + bar.volume, 0) : null;
      return {
        date: last.date,
        startDate: first.date,
        open: hasRange ? first.open : null,
        high: hasRange ? Math.max(...group.map(({ high }) => high)) : null,
        low: hasRange ? Math.min(...group.map(({ low }) => low)) : null,
        close: last.close,
        volume: volumeValue(volume),
      };
    });
  }

  function movingAverage(values, period) {
    let sum = 0;
    return values.map((value, index) => {
      sum += value;
      if (index >= period) sum -= values[index - period];
      return index >= period - 1 ? sum / period : null;
    });
  }

  function exponentialAverage(values, period) {
    const weight = 2 / (period + 1);
    let average = null;
    let count = 0;
    let sum = 0;
    return values.map((value) => {
      if (value === null) {
        count = 0;
        sum = 0;
        average = null;
      } else if (average !== null) {
        average += weight * (value - average);
      } else {
        sum += value;
        count += 1;
        if (count === period) average = sum / period;
      }
      return average;
    });
  }

  function stochastic(bars) {
    const lookback = 9;
    let consecutive = 0;
    let k = 50;
    let d = 50;
    return bars.map((bar, index) => {
      if (bar.low === null || bar.high === null) {
        consecutive = 0;
        k = 50;
        d = 50;
        return { k: null, d: null };
      }
      consecutive += 1;
      if (consecutive < lookback) return { k: null, d: null };
      const window = bars.slice(index - lookback + 1, index + 1);
      const high = Math.max(...window.map((point) => point.high));
      const low = Math.min(...window.map((point) => point.low));
      const rsv = high === low ? 50 : 100 * (bar.close - low) / (high - low);
      k += (rsv - k) / 3;
      d += (k - d) / 3;
      return { k, d };
    });
  }

  function buildSeries(history = [], candles = [], period = "daily") {
    if (!["daily", "weekly", "monthly"].includes(period)) throw new RangeError("Unsupported period");
    const bars = aggregate(dailyBars(history, candles), period);
    const closes = bars.map(({ close }) => close);
    const averages = MA_PERIODS.map((length) => movingAverage(closes, length));
    const kd = stochastic(bars);
    const fast = exponentialAverage(closes, 12);
    const slow = exponentialAverage(closes, 26);
    const dif = fast.map((value, i) => slow[i] === null ? null : value - slow[i]);
    const signals = exponentialAverage(dif, 9);
    return bars.map((bar, i) => ({
      ...bar,
      ma: Object.fromEntries(MA_PERIODS.map((length, j) => [length, averages[j][i]])),
      ...kd[i],
      dif: dif[i],
      signal: signals[i],
      histogram: signals[i] === null ? null : dif[i] - signals[i],
    }));
  }

  return Object.freeze({ buildSeries, MA_PERIODS });
})();
