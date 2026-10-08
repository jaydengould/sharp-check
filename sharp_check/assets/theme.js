// Theme: applied before Dash renders to avoid a flash; the picker's clientside callback keeps it in sync.
(function () {
  try {
    var t = localStorage.getItem("sharp-theme");
    if (t && t !== "system") document.documentElement.dataset.theme = t;
  } catch (e) { /* storage blocked: System theme */ }
})();

window.dash_clientside = Object.assign({}, window.dash_clientside, {
  sharp: {
    // Sets data-theme, remembers it, and restyles both charts from the computed CSS variables (Plotly can't read CSS).
    theme: function (theme, figA, figB) {
      var root = document.documentElement;
      if (theme && theme !== "system") root.dataset.theme = theme; else delete root.dataset.theme;
      try { localStorage.setItem("sharp-theme", theme || "system"); } catch (e) { /* not remembered */ }
      var css = getComputedStyle(root);
      var v = function (name) { return css.getPropertyValue(name).trim(); };
      function paint(fig) {
        if (!fig) return window.dash_clientside.no_update;
        var f = JSON.parse(JSON.stringify(fig));
        var lay = f.layout || {};
        f.layout = Object.assign({}, lay, {
          paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
          font: Object.assign({}, lay.font, { color: v("--fg") }),
          hoverlabel: { bgcolor: v("--card"), bordercolor: v("--line"), font: { color: v("--fg") } },
          xaxis: Object.assign({}, lay.xaxis, { gridcolor: v("--line"), linecolor: v("--line") }),
          yaxis: Object.assign({}, lay.yaxis, { gridcolor: v("--line"), zerolinecolor: v("--muted") })
        });
        (f.data || []).forEach(function (tr, i) {
          tr.line = Object.assign({}, tr.line, { color: v("--series-" + (i + 1)) });
        });
        return f;
      }
      return [theme, paint(figA), paint(figB)];
    }
  }
});
