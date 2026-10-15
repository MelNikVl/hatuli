// Shared by the dashboard and Investments map: higher rental yield is better.
window.YieldHeat = (() => {
  const stops = [[239, 68, 68], [250, 204, 21], [134, 239, 172], [22, 163, 74], [20, 83, 45]];

  function scale(values) {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    if (!sorted.length) return {lo: 0, hi: 0};
    let lo = sorted[Math.floor(sorted.length * 0.10)];
    let hi = sorted[Math.min(Math.floor(sorted.length * 0.90), sorted.length - 1)];
    // A repeated median can collapse the percentile range despite real differences.
    if (hi <= lo) { lo = sorted[0]; hi = sorted[sorted.length - 1]; }
    return {lo, hi};
  }

  function style(value, range) {
    const t = range.hi > range.lo
      ? Math.max(0, Math.min(1, (value - range.lo) / (range.hi - range.lo))) : 0.5;
    const pos = t * (stops.length - 1);
    const i = Math.min(Math.floor(pos), stops.length - 2);
    const color = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * (pos - i)));
    const best = range.hi > range.lo && value >= range.hi;
    return {
      color: best ? '#14532d' : '#fff', weight: best ? 1.5 : 0.5,
      fillColor: `rgb(${color[0]},${color[1]},${color[2]})`, fillOpacity: best ? 0.85 : 0.65,
    };
  }

  function tooltip(value, count, range) {
    const best = range.hi > range.lo && value >= range.hi;
    return `${best ? '<b>Лучшая доходность</b><br>' : ''}медиана доходности: ${value.toFixed(1)}% · ${count} объявл.`;
  }

  return {scale, style, tooltip};
})();
