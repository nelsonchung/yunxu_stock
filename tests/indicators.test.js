"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
require("../static/indicators.js");

const { buildSeries } = globalThis.StockIndicators;
const day = (index) => new Date(Date.UTC(2025, 0, 1 + index)).toISOString().slice(0, 10);
const near = (actual, expected) => {
  assert.ok(Math.abs(actual - expected) < 1e-9, `Expected ${actual} to be near ${expected}`);
};

test("ISO weeks span month and year boundaries while incomplete bars retain close only", () => {
  const history = [["2025-01-03", 15], ["2025-01-30", 20], ["2025-02-30", 80], ["2024-12-27", 9]];
  const candles = [
    ["2024-12-30", 11, 14, 10, 12, 100],
    ["2025-01-02", 14, 18, 13, 16, 300],
    ["2025-01-31", 21, 23, 19, 22, 500],
  ];
  const weekly = buildSeries(history, candles, "weekly");
  assert.deepEqual(weekly.map((bar) => [bar.startDate, bar.date, bar.open, bar.close, bar.volume]), [
    ["2024-12-27", "2024-12-27", null, 9, null],
    ["2024-12-30", "2025-01-03", null, 15, null],
    ["2025-01-30", "2025-01-31", null, 22, null],
  ]);
  const monthly = buildSeries(history, candles, "monthly");
  assert.deepEqual(monthly.map((bar) => [bar.startDate, bar.date, bar.high, bar.low]), [
    ["2024-12-27", "2024-12-30", null, null],
    ["2025-01-02", "2025-01-31", null, null],
  ]);

  const [bar] = buildSeries([], candles.slice(0, 2), "weekly");
  assert.deepEqual([bar.startDate, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume],
                   ["2024-12-30", "2025-01-02", 11, 18, 10, 16, 400]);
});

test("SMA and MACD warm up on close-only data without implying candle data", () => {
  const history = Array.from({ length: 242 }, (_, i) => [day(i), i + 1]);
  const bars = buildSeries(history);
  assert.equal(bars[3].ma[5], null);
  assert.equal(bars[4].ma[5], 3);
  assert.equal(bars[5].ma[5], 4);
  assert.equal(bars[238].ma[240], null);
  assert.equal(bars[239].ma[240], 120.5);
  assert.equal(bars[240].ma[240], 121.5);

  assert.equal(bars[24].dif, null);
  near(bars[25].dif, 7);
  assert.equal(bars[32].signal, null);
  near(bars[33].signal, 7);
  near(bars[33].histogram, 0);
  assert.equal(bars[33].k, null);
  assert.equal(bars[33].volume, null);

  history[34][1] = 48;
  const afterJump = buildSeries(history)[34];
  near(afterJump.dif, 7 + 2 - 26 / 27);
  near(afterJump.histogram, 0.8 * (2 - 26 / 27));
});

test("KD seeds from 50 and resets its nine-bar window across close-only gaps", () => {
  const candles = Array.from({ length: 19 }, (_, i) => [day(i), 80, 100, 20, 100, 15]);
  candles.splice(9, 1);
  const bars = buildSeries([[day(9), 80]], candles);
  assert.equal(bars[7].k, null);
  near(bars[8].k, 200 / 3);
  near(bars[8].d, 500 / 9);
  assert.equal(bars[9].k, null);
  assert.equal(bars[17].k, null);
  near(bars[18].k, bars[8].k);
  near(bars[18].d, bars[8].d);

  const flat = Array.from({ length: 9 }, (_, i) => [day(i), 25, 25, 25, 25, 0]);
  const last = buildSeries([], flat).at(-1);
  assert.equal(last.k, 50);
  assert.equal(last.d, 50);
  assert.equal(last.volume, 0);
});

test("Custom dates clip weekly and monthly candles but retain indicator history", () => {
  const candles = Array.from({ length: 46 }, (_, i) => [day(i), i + 10, i + 12, i + 9, i + 11, 10]);
  const range = { start: "2025-02-04", end: "2025-02-05" };
  const daily = buildSeries([], candles, "daily", range);
  assert.equal(daily.at(-1).date, range.end);
  assert.equal(daily.at(-2).ma[5], 43);
  for (const period of ["weekly", "monthly"]) {
    const series = buildSeries([], candles, period, range);
    const last = series.at(-1);
    assert.deepEqual([last.startDate, last.date, last.open, last.high, last.low, last.close, last.volume],
                     [range.start, range.end, 44, 47, 43, 46, 20]);
    assert.ok(series.at(-2).date < range.start);
  }
});
