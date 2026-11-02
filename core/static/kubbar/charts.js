/* Kubbar storytelling charts: one renderer for every dashboard (Chart.js 4).
   Thin marks, recessive grid, legend only for two or more series, values
   formatted in the chart's unit, a tooltip on every mark. */
(function () {
  const INK = "#52514e", MUTED = "#898781", GRID = "#e1e0d9";
  function compact(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return Math.round(v / 1e3) + "K";
    return Math.round(v).toLocaleString("en-US");
  }
  function full(v, unit) {
    if (unit === "%") return v.toFixed(1) + "%";
    const n = Math.round(v).toLocaleString("en-US");
    return unit ? n + " " + unit : n;
  }
  function render(spec) {
    const el = document.getElementById("chart-" + spec.id);
    if (!el || !window.Chart) return;
    const horizontal = spec.type === "hbar";
    const isLine = spec.type === "line";
    const many = spec.series.length > 1;
    const datasets = spec.series.map(s => {
      const color = s.color || "#2a78d6";
      if (isLine) {
        return {label: s.name, data: s.data, borderColor: color, backgroundColor: color + "1a",
                borderWidth: 2, tension: .25, pointRadius: 0, pointHoverRadius: 5,
                pointHitRadius: 12, fill: !many, borderCapStyle: "round"};
      }
      return {label: s.name, data: s.data, backgroundColor: s.colors || color,
              borderRadius: 4, borderSkipped: "start", maxBarThickness: 24,
              categoryPercentage: .8, barPercentage: many ? .9 : .8,
              borderColor: "#fcfcfb", borderWidth: many ? {left: 1, right: 1} : 0};
    });
    const valueAxis = {beginAtZero: true, border: {display: false},
      grid: {color: GRID, lineWidth: 1, drawTicks: false},
      ticks: {color: MUTED, padding: 6, maxTicksLimit: 6,
              callback: v => spec.unit === "%" ? v + "%" : compact(v)}};
    const catAxis = {grid: {display: false}, border: {color: "#c3c2b7"},
      ticks: {color: INK, autoSkip: !horizontal, maxRotation: 0,
              callback: function (v) {
                const l = this.getLabelForValue(v);
                return horizontal && l.length > 28 ? l.slice(0, 27) + "…" : l; }}};
    new Chart(el, {
      type: isLine ? "line" : "bar",
      data: {labels: spec.labels, datasets},
      options: {
        indexAxis: horizontal ? "y" : "x", responsive: true, maintainAspectRatio: false,
        interaction: {mode: isLine ? "index" : "nearest", intersect: !isLine, axis: horizontal ? "y" : "x"},
        plugins: {
          legend: {display: many, position: "top", align: "start",
                   labels: {color: INK, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 10}},
          tooltip: {backgroundColor: "#0b0b0b", padding: 10, cornerRadius: 8,
                    callbacks: {label: c => " " + c.dataset.label + ": " +
                                full(horizontal ? c.parsed.x : c.parsed.y, spec.unit)}}},
        scales: horizontal ? {x: valueAxis, y: catAxis} : {x: catAxis, y: valueAxis}}
    });
  }
  window.VetrexCharts = {render, renderAll: specs => specs.forEach(render)};
})();
