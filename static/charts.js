(function () {
  const data = window.APP_DATA || { perDay: [], topTables: [] };

  function themed() {
    const dark = document.documentElement.classList.contains("dark");
    return {
      axis:  dark ? "rgba(228,228,231,0.65)" : "rgba(63,63,70,0.65)",
      grid:  dark ? "rgba(63,63,70,0.35)"   : "rgba(228,228,231,0.75)",
      line:  dark ? "rgba(129,140,248,0.95)" : "rgba(79,70,229,0.95)",
      fill:  dark ? "rgba(129,140,248,0.18)" : "rgba(79,70,229,0.14)",
      bar:   dark ? "rgba(129,140,248,0.85)" : "rgba(79,70,229,0.85)",
      tip:   dark ? "rgba(39,39,42,0.95)"   : "rgba(24,24,27,0.95)"
    };
  }

  let jobsChart, topChart;

  function commonOpts(tokens) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: tokens.tip,
          titleFont: { family: "Inter", weight: 600, size: 12 },
          bodyFont:  { family: "JetBrains Mono", size: 11 },
          padding: 10,
          cornerRadius: 8,
          displayColors: false
        }
      }
    };
  }

  function buildJobs() {
    const el = document.getElementById("jobsChart");
    if (!el) return;
    const t = themed();
    const labels = data.perDay.map(d => d.date.slice(5));
    const values = data.perDay.map(d => d.count);

    if (jobsChart) jobsChart.destroy();
    jobsChart = new Chart(el.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Jobs",
          data: values,
          borderColor: t.line,
          backgroundColor: t.fill,
          fill: true,
          tension: 0.38,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointHoverBackgroundColor: t.line,
          pointHoverBorderColor: "#fff",
          pointHoverBorderWidth: 2,
          borderWidth: 2.5
        }]
      },
      options: Object.assign(commonOpts(t), {
        scales: {
          x: {
            ticks: { color: t.axis, maxRotation: 0, autoSkip: true, maxTicksLimit: 8, font: { size: 10, family: "Inter" } },
            grid: { display: false }
          },
          y: {
            beginAtZero: true,
            ticks: { color: t.axis, precision: 0, font: { size: 10, family: "Inter" } },
            grid: { color: t.grid, drawTicks: false }
          }
        }
      })
    });
  }

  function buildTop() {
    const el = document.getElementById("topTablesChart");
    if (!el || !data.topTables || !data.topTables.length) return;
    const t = themed();
    const labels = data.topTables.map(x => x.table);
    const values = data.topTables.map(x => x.count);

    if (topChart) topChart.destroy();
    topChart = new Chart(el.getContext("2d"), {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "Count",
          data: values,
          backgroundColor: t.bar,
          borderRadius: 6,
          barThickness: 14
        }]
      },
      options: Object.assign(commonOpts(t), {
        indexAxis: "y",
        scales: {
          x: {
            beginAtZero: true,
            ticks: { color: t.axis, precision: 0, font: { size: 10, family: "Inter" } },
            grid: { color: t.grid, drawTicks: false }
          },
          y: {
            ticks: { color: t.axis, font: { size: 10, family: "JetBrains Mono" } },
            grid: { display: false }
          }
        }
      })
    });
  }

  function rebuild() { buildJobs(); buildTop(); }
  document.addEventListener("DOMContentLoaded", rebuild);
  window.addEventListener("themechange", rebuild);
})();
