// Dashboard weather + river-level card.
//
// Progressive enhancement over an empty server-rendered shell: fetch the
// server-cached snapshot from /api/weather (the browser never talks to the
// weather APIs directly) and paint it. Re-polls slowly; the server does the
// real refresh on its own timer, so this is just to pick up new values.
(function () {
  const card = document.querySelector('[data-weather]');
  if (!card) return;

  const POLL_MS = 5 * 60 * 1000;
  const $ = (sel) => card.querySelector(sel);

  function fmt(metric, digits) {
    if (!metric || metric.value == null) return null;
    const n = Number(metric.value);
    const v = Number.isFinite(n) ? (digits != null ? n.toFixed(digits) : n) : metric.value;
    return `${v}${metric.unit || ''}`;
  }

  function metricRow(label, text) {
    if (text == null) return '';
    return `<div class="wx-metric"><span class="wx-k">${label}</span>` +
           `<span class="wx-v">${text}</span></div>`;
  }

  // Wind and gusts on one line: "8 / 14 mph SSE" (or just "8 mph SSE" if no gust).
  function windText(w) {
    const s = w.wind_speed;
    if (!s || s.value == null) return null;
    const unit = s.unit || '';
    const dir = (w.wind_direction && w.wind_direction.compass)
      ? ' ' + w.wind_direction.compass : '';
    const speed = Math.round(Number(s.value));
    const g = w.wind_gust;
    const gust = (g && g.value != null) ? ` / ${Math.round(Number(g.value))}` : '';
    return `${speed}${gust} ${unit}${dir}`;
  }

  function renderWeather(w) {
    if (!w) return;
    $('[data-wx-icon]').textContent = (w.condition && w.condition.icon) || '🌡️';
    $('[data-wx-temp]').textContent = fmt(w.temperature, 0) || '—';
    $('[data-wx-cond]').textContent = (w.condition && w.condition.text) || '';

    const unit = w.temp_unit || '°';
    const hilo = [];
    if (w.high != null) hilo.push(`H ${Math.round(w.high)}${unit}`);
    if (w.low != null) hilo.push(`L ${Math.round(w.low)}${unit}`);
    if (w.apparent && w.apparent.value != null) {
      hilo.push(`feels ${fmt(w.apparent, 0)}`);
    }
    $('[data-wx-hilo]').textContent = hilo.join(' · ');

    $('[data-wx-metrics]').innerHTML = [
      metricRow('Wind', windText(w)),
      metricRow('Humidity', fmt(w.humidity, 0)),
      metricRow('Dew point', fmt(w.dew_point, 0)),
      metricRow('Precip', fmt(w.precipitation, 2)),
      metricRow('Pressure', fmt(w.pressure, 2)),
      metricRow('UV index', fmt(w.uv_index, 0)),
    ].join('');
  }

  const TREND = { rising: '↑ rising', falling: '↓ falling', steady: '→ steady' };

  function renderWater(water) {
    const box = $('[data-wx-water]');
    if (!water || !water.level || water.level.value == null) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    $('[data-wx-water-label]').textContent = water.label || 'River level';
    $('[data-wx-level]').textContent =
      `${Number(water.level.value).toFixed(2)} ${water.level.unit || 'ft'}`;

    const trendEl = $('[data-wx-trend]');
    trendEl.textContent = water.trend ? TREND[water.trend] || '' : '';
    trendEl.className = 'wx-trend' + (water.trend ? ' wx-trend-' + water.trend : '');

    const flood = $('[data-wx-flood]');
    flood.textContent = water.flood_label || '';
    // Anything other than the normal "no_flooding" state gets the alert color.
    flood.className = 'wx-flood' +
      (water.flood_category && water.flood_category !== 'no_flooding' ? ' wx-flood-alert' : '');

    sparkline(water.series || []);
  }

  function sparkline(values) {
    const svg = $('[data-wx-spark]');
    const line = $('[data-wx-spark-line]');
    if (!values || values.length < 2) { svg.hidden = true; return; }
    const W = 120, H = 32, pad = 2;
    const min = Math.min(...values), max = Math.max(...values);
    const span = max - min || 1;
    const pts = values.map((v, i) => {
      const x = pad + (i / (values.length - 1)) * (W - 2 * pad);
      const y = H - pad - ((v - min) / span) * (H - 2 * pad);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    line.setAttribute('points', pts.join(' '));
    svg.hidden = false;
  }

  function render(data) {
    if (!data || !data.enabled) { card.hidden = true; return; }
    card.hidden = false;

    $('[data-wx-loc]').textContent = data.location || '';
    const updated = $('[data-wx-updated]');
    updated.textContent = data.updated
      ? 'updated ' + new Date(data.updated * 1000).toLocaleTimeString([],
          { hour: 'numeric', minute: '2-digit' })
      : 'loading…';

    renderWeather(data.weather);
    renderWater(data.water);

    const err = $('[data-wx-error]');
    if (!data.weather && !data.water && data.updated) {
      err.hidden = false;
      err.textContent = 'Weather feed unavailable right now.';
    } else {
      err.hidden = true;
    }
  }

  async function poll() {
    try {
      const r = await fetch('/api/weather');
      if (r.ok) render(await r.json());
    } catch (_) { /* keep whatever's on screen */ }
  }

  poll();
  setInterval(poll, POLL_MS);
})();
