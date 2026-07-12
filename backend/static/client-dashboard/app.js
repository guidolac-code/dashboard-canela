const state = {
  range: "7d",
  module: "meta",
  data: null,
};

const fmtNumber = new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 });
const fmtDecimal = new Intl.NumberFormat("es-AR", { maximumFractionDigits: 2 });
const fmtCurrency = new Intl.NumberFormat("es-AR", {
  style: "currency",
  currency: "ARS",
  maximumFractionDigits: 0,
});

function formatValue(value, format) {
  if (format === "currency") return fmtCurrency.format(value || 0);
  if (format === "percent") return `${fmtDecimal.format(value || 0)}%`;
  if (format === "ratio") return fmtDecimal.format(value || 0);
  if (format === "text") return escapeHtml(value || "");
  return fmtNumber.format(value || 0);
}

function formatDelta(delta) {
  if (delta === null || delta === undefined) return "Sin base previa";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${fmtDecimal.format(delta)}% vs período anterior`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function pointsForLine(values, width, height, pad, maxOverride) {
  const max = Math.max(maxOverride || 0, ...values, 1);
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      const x = pad + index * step;
      const y = height - pad - (value / max) * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function renderSparkline(series, key, target) {
  const values = series.map((row) => Number(row[key] || 0));
  if (!values.length) return "";
  const width = 180;
  const height = 28;
  const pad = 2;
  const max = Math.max(...values, target || 0, 1);
  const points = pointsForLine(values, width, height, pad, max);
  return `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
    <polyline points="${points}" fill="none" stroke="#6da8ff" stroke-width="1.6" vector-effect="non-scaling-stroke" />
  </svg>`;
}

function renderKpis(data) {
  const container = document.getElementById("kpis");
  container.innerHTML = data.kpis
    .map((kpi) => {
      const deltaClass = kpi.delta_pct > 0 ? "up" : kpi.delta_pct < 0 ? "down" : "";
      const delta = kpi.delta_pct === undefined ? "" : `<div class="kpi-delta ${deltaClass}">${formatDelta(kpi.delta_pct)}</div>`;
      const spark = data.series ? renderSparkline(data.series, kpi.key, kpi.key === "roas" ? data.account.roas_objetivo : null) : "";
      return `<article class="kpi-card">
        <div class="kpi-label">${escapeHtml(kpi.label)}</div>
        <div class="kpi-value">${formatValue(kpi.value, kpi.format)}</div>
        ${delta}
        <div class="sparkline">${spark}</div>
      </article>`;
    })
    .join("");
}

function renderLineChart(el, series, key, options = {}) {
  const width = 700;
  const height = 250;
  const pad = 28;
  const values = series.map((row) => Number(row[key] || 0));
  const target = options.target || 0;
  const max = Math.max(...values, target, 1);
  const points = pointsForLine(values, width, height, pad, max);
  const targetY = height - pad - (target / max) * (height - pad * 2);
  const labels = series
    .map((row, index) => {
      if (index !== 0 && index !== series.length - 1) return "";
      const x = pad + (series.length > 1 ? index * ((width - pad * 2) / (series.length - 1)) : 0);
      return `<text x="${x}" y="${height - 6}" text-anchor="${index === 0 ? "start" : "end"}">${row.date.slice(5)}</text>`;
    })
    .join("");

  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">
    <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#24272d" />
    ${target ? `<line x1="${pad}" y1="${targetY}" x2="${width - pad}" y2="${targetY}" stroke="#3ddc84" stroke-dasharray="4 5" />` : ""}
    <polyline points="${points}" fill="none" stroke="#6da8ff" stroke-width="2" vector-effect="non-scaling-stroke" />
    <g fill="#5d6570" font-size="11">${labels}</g>
  </svg>`;
}

function renderBarChart(el, series, key) {
  const width = 700;
  const height = 250;
  const pad = 28;
  const values = series.map((row) => Number(row[key] || 0));
  const max = Math.max(...values, 1);
  const slot = (width - pad * 2) / Math.max(values.length, 1);
  const bars = values
    .map((value, index) => {
      const barHeight = (value / max) * (height - pad * 2);
      const x = pad + index * slot + slot * 0.18;
      const y = height - pad - barHeight;
      return `<rect x="${x}" y="${y}" width="${Math.max(slot * 0.64, 4)}" height="${barHeight}" rx="3" fill="#6da8ff" />`;
    })
    .join("");
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">
    <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#24272d" />
    ${bars}
  </svg>`;
}

function renderFunnel(data) {
  const max = Math.max(...data.funnel.map((row) => row.value), 1);
  document.getElementById("funnel").innerHTML = data.funnel
    .map((row) => {
      const width = Math.max((row.value / max) * 100, row.value ? 4 : 0);
      const drop = row.drop_pct === null ? "" : `${fmtDecimal.format(row.drop_pct)}% caída`;
      return `<div class="funnel-row">
        <span>${escapeHtml(row.label)}</span>
        <div class="funnel-bar"><div class="funnel-fill" style="width:${width}%"></div></div>
        <span class="funnel-value">${fmtNumber.format(row.value)}</span>
        <span></span><span>${drop}</span><span></span>
      </div>`;
    })
    .join("");
}

function renderFormatDistribution(data) {
  const totalSpend = data.format_distribution.reduce((sum, row) => sum + Number(row.spend || 0), 0) || 1;
  document.getElementById("formatDistribution").innerHTML = data.format_distribution
    .map((row) => {
      const width = (Number(row.spend || 0) / totalSpend) * 100;
      return `<div class="format-row">
        <div class="format-meta">
          <strong>${escapeHtml(row.format)}</strong>
          <span>${fmtNumber.format(row.ads)} ads · ${fmtCurrency.format(row.spend)}</span>
        </div>
        <div class="format-bar"><div class="format-fill" style="width:${width}%"></div></div>
      </div>`;
    })
    .join("");
}

function renderTopAds(data) {
  const rows = data.top_ads.length
    ? data.top_ads
        .map(
          (ad) => `<tr>
            <td class="truncate" title="${escapeHtml(ad.name)}">${escapeHtml(ad.name)}</td>
            <td class="truncate" title="${escapeHtml(ad.adset)}">${escapeHtml(ad.adset)}</td>
            <td class="num">${fmtNumber.format(ad.purchases)}</td>
            <td class="num">${fmtDecimal.format(ad.roas)}</td>
            <td class="num">${fmtCurrency.format(ad.purchase_value)}</td>
            <td class="num">${fmtDecimal.format(ad.ctr_link)}%</td>
          </tr>`,
        )
        .join("")
    : `<tr><td colspan="6" class="empty">Sin ads con datos en este período.</td></tr>`;
  document.getElementById("topAds").innerHTML = rows;
}

function renderRecentChanges(data) {
  const rows = data.recent_changes.length
    ? data.recent_changes
        .map(
          (ad) => `<tr>
            <td class="truncate" title="${escapeHtml(ad.name)}">${escapeHtml(ad.name)}</td>
            <td class="truncate" title="${escapeHtml(ad.adset)}">${escapeHtml(ad.adset)}</td>
            <td>${escapeHtml(ad.status)}</td>
            <td>${escapeHtml(ad.updated_at)}</td>
          </tr>`,
        )
        .join("")
    : `<tr><td colspan="4" class="empty">Sin ads creados en este período.</td></tr>`;
  document.getElementById("recentChanges").innerHTML = rows;
}

function renderFatigue(data) {
  const el = document.getElementById("fatigueSignals");
  if (!data.fatigue_signals.length) {
    el.innerHTML = `<div class="empty">No hay señales fuertes de fatiga para este rango.</div>`;
    return;
  }
  el.innerHTML = data.fatigue_signals
    .map(
      (item) => `<div class="signal">
        <div><strong>${escapeHtml(item.name)}</strong><br />CTR ${fmtDecimal.format(item.ctr_previous)}% → ${fmtDecimal.format(item.ctr_current)}%</div>
        <span>Frec. ${fmtDecimal.format(item.frequency)}</span>
      </div>`,
    )
    .join("");
}

function renderEngagement(data) {
  const metrics = [
    ["Seguimientos IG", data.engagement.instagram_follows],
    ["Comentarios", data.engagement.comments],
    ["Reacciones", data.engagement.reactions],
    ["Video 3s", data.engagement.video_3s],
  ];
  document.getElementById("engagement").innerHTML = metrics
    .map((metric) => `<div class="mini-metric"><span>${metric[0]}</span><strong>${fmtNumber.format(metric[1] || 0)}</strong></div>`)
    .join("");
}

function setModuleSections(module) {
  document.querySelectorAll(".module-section").forEach((section) => {
    section.hidden = section.dataset.moduleSection !== module;
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    if (button.dataset.module === module) button.classList.add("active");
    else button.classList.remove("active");
  });
}

function updateRangeControlsForModule(module) {
  const controls = document.querySelector(".range-controls");
  controls.hidden = module === "creators";
}

function renderMeta(data) {
  state.data = data;
  setModuleSections("meta");
  updateRangeControlsForModule("meta");
  document.getElementById("rangeLabel").textContent = `${data.range.current.since} a ${data.range.current.until}`;
  document.getElementById("cacheLabel").textContent = data.cached_at ? `Actualizado ${new Date(data.cached_at).toLocaleString("es-AR")}` : "";
  document.getElementById("roasTarget").textContent = data.account.roas_objetivo ? `Objetivo ${fmtDecimal.format(data.account.roas_objetivo)}` : "Sin objetivo";
  renderKpis(data);
  renderLineChart(document.getElementById("roasChart"), data.series, "roas", { target: data.account.roas_objetivo || 0 });
  renderBarChart(document.getElementById("purchaseChart"), data.series, "purchases");
  renderFunnel(data);
  renderFormatDistribution(data);
  document.getElementById("paretoText").textContent = data.pareto.text;
  renderTopAds(data);
  renderRecentChanges(data);
  renderFatigue(data);
  renderEngagement(data);
}

function renderMetricRows(tbodyId, rows, emptyText, valueKey = "purchase_value") {
  const body = document.getElementById(tbodyId);
  body.innerHTML = rows.length
    ? rows
        .map((row) => `<tr>
          <td class="truncate" title="${escapeHtml(row.creator || row.product)}">${escapeHtml(row.creator || row.product)}</td>
          <td class="num">${fmtNumber.format(row.active_ads || 0)}</td>
          <td class="num">${fmtNumber.format(row.purchases || 0)}</td>
          <td class="num">${fmtDecimal.format(row.roas || 0)}</td>
          <td class="num">${valueKey === "spend" ? fmtCurrency.format(row.spend || 0) : fmtCurrency.format(row.purchase_value || 0)}</td>
        </tr>`)
        .join("")
    : `<tr><td colspan="5" class="empty">${escapeHtml(emptyText)}</td></tr>`;
}

function renderCreatorsTable(rows) {
  document.getElementById("creatorsTable").innerHTML = rows.length
    ? rows
        .map((row) => `<tr>
          <td class="truncate" title="${escapeHtml(row.creator)}">${escapeHtml(row.creator)}</td>
          <td class="num">${fmtNumber.format(row.active_ads || 0)}</td>
          <td class="num">${fmtDecimal.format(row.participation_pct || 0)}%</td>
          <td class="num">${fmtNumber.format(row.purchases || 0)}</td>
          <td class="num">${fmtDecimal.format(row.roas || 0)}</td>
        </tr>`)
        .join("")
    : `<tr><td colspan="5" class="empty">Sin creadoras clasificadas.</td></tr>`;
}

function renderUnclassified(rows) {
  document.getElementById("unclassifiedAds").innerHTML = rows.length
    ? rows
        .map((row) => `<tr>
          <td class="truncate" title="${escapeHtml(row.ad_name)}">${escapeHtml(row.ad_name)}</td>
          <td class="truncate" title="${escapeHtml(row.adset)}">${escapeHtml(row.adset)}</td>
          <td class="truncate" title="${escapeHtml(row.campaign)}">${escapeHtml(row.campaign)}</td>
          <td>${escapeHtml(row.reason)}</td>
        </tr>`)
        .join("")
    : `<tr><td colspan="4" class="empty">No hay ads sin clasificar.</td></tr>`;
}

function formatDateTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("es-AR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function renderCreators(data) {
  state.data = data;
  setModuleSections("creators");
  updateRangeControlsForModule("creators");
  document.getElementById("rangeLabel").textContent = `Últimos 30 días · ${data.range.current.since} a ${data.range.current.until}`;
  document.getElementById("cacheLabel").textContent = data.cached_at ? `Actualizado ${new Date(data.cached_at).toLocaleString("es-AR")}` : "";
  renderKpis(data);
  renderMetricRows("topBeboteo", data.top_beboteo, "Sin Beboteo con ventas en los últimos 30 días.");
  renderMetricRows("topNarrado", data.top_narrado, "Sin Narrado con ventas en los últimos 30 días.");
  renderMetricRows("topGeneralCreators", data.top_general, "Sin creadoras con ventas en los últimos 30 días.");
  renderMetricRows("topProducts", data.top_products, "Sin productos clasificados.");
  renderCreatorsTable(data.creators_table);
  document.getElementById("detectedCreators").innerHTML = data.detected_creators.length
    ? data.detected_creators.map((name) => `<span class="tag">${escapeHtml(name)}</span>`).join("")
    : `<div class="empty">Sin creadoras detectadas.</div>`;
  renderUnclassified(data.unclassified_ads);
}

function renderLatestOrders(rows) {
  document.getElementById("latestOrders").innerHTML = rows.length
    ? rows
        .map((order) => `<tr>
          <td>${escapeHtml(order.number)}</td>
          <td>${escapeHtml(formatDateTime(order.date))}</td>
          <td class="truncate" title="${escapeHtml(order.customer)}">${escapeHtml(order.customer)}</td>
          <td class="num">${fmtCurrency.format(order.total || 0)}</td>
          <td>${escapeHtml(order.payment_status)}</td>
          <td>${escapeHtml(order.coupon || "-")}</td>
        </tr>`)
        .join("")
    : `<tr><td colspan="6" class="empty">Sin órdenes en este período.</td></tr>`;
}

function renderBusinessProducts(tbodyId, rows, emptyText) {
  document.getElementById(tbodyId).innerHTML = rows.length
    ? rows
        .map((row) => `<tr>
          <td class="truncate" title="${escapeHtml(row.product)}">${escapeHtml(row.product)}</td>
          <td class="num">${fmtNumber.format(row.units || 0)}</td>
          <td class="num">${fmtCurrency.format(row.revenue || 0)}</td>
        </tr>`)
        .join("")
    : `<tr><td colspan="3" class="empty">${escapeHtml(emptyText)}</td></tr>`;
}

function renderPaymentStatus(rows) {
  document.getElementById("paymentStatus").innerHTML = rows.length
    ? rows
        .map((row) => `<div class="signal">
          <div><strong>${escapeHtml(row.status)}</strong><br />${fmtNumber.format(row.orders || 0)} órdenes</div>
          <span>${fmtCurrency.format(row.revenue || 0)}</span>
        </div>`)
        .join("")
    : `<div class="empty">Sin estados de pago en este período.</div>`;
}

function renderBusinessCustomers(customers) {
  const el = document.getElementById("businessCustomers");
  if (!customers) {
    el.innerHTML = `<div class="empty">Tiendanube no devolvió datos suficientes para distinguir clientes nuevos y recurrentes.</div>`;
    return;
  }
  el.innerHTML = [
    ["Nuevos", customers.new],
    ["Recurrentes", customers.recurrent],
    ["Total clientes", customers.total],
  ]
    .map((metric) => `<div class="mini-metric"><span>${metric[0]}</span><strong>${fmtNumber.format(metric[1] || 0)}</strong></div>`)
    .join("");
}

function renderBusiness(data) {
  state.data = data;
  setModuleSections("business");
  updateRangeControlsForModule("business");
  document.getElementById("rangeLabel").textContent = `${data.range.current.since} a ${data.range.current.until}`;
  document.getElementById("cacheLabel").textContent = data.cached_at ? `Actualizado ${new Date(data.cached_at).toLocaleString("es-AR")}` : "";
  renderKpis(data);
  renderBarChart(document.getElementById("revenueChart"), data.series, "revenue");
  renderBarChart(document.getElementById("ordersChart"), data.series, "orders");
  renderLatestOrders(data.latest_orders);
  renderPaymentStatus(data.payment_status);
  renderBusinessProducts("businessProductsUnits", data.top_products_units, "Sin productos vendidos en este período.");
  renderBusinessProducts("businessProductsRevenue", data.top_products_revenue, "Sin facturación de productos en este período.");
  renderBusinessCustomers(data.customers);
}

async function loadDashboard() {
  const params = new URLSearchParams({ range: state.module === "creators" ? "30d" : state.range });
  if (state.module !== "creators" && state.range === "custom") {
    params.set("since", document.getElementById("since").value);
    params.set("until", document.getElementById("until").value);
  }
  document.getElementById("rangeLabel").textContent = "Cargando datos...";
  const endpoint = state.module === "creators" ? "creators" : state.module === "business" ? "business" : "meta";
  const response = await fetch(`/api/client-dashboard/${endpoint}?${params.toString()}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "No se pudo cargar el dashboard.");
  }
  const data = await response.json();
  if (state.module === "creators") renderCreators(data);
  else if (state.module === "business") renderBusiness(data);
  else renderMeta(data);
}

function setDefaultDates() {
  const until = new Date();
  const since = new Date();
  since.setDate(until.getDate() - 6);
  document.getElementById("until").value = until.toISOString().slice(0, 10);
  document.getElementById("since").value = since.toISOString().slice(0, 10);
}

function showError(error) {
  document.getElementById("rangeLabel").textContent = error.message;
  document.getElementById("kpis").innerHTML = "";
}

document.querySelectorAll(".range-button").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".range-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.range = button.dataset.range;
    document.querySelector(".custom-range").hidden = state.range !== "custom";
    if (state.range !== "custom") {
      try {
        await loadDashboard();
      } catch (error) {
        showError(error);
      }
    }
  });
});

document.querySelectorAll(".nav-item:not(.disabled)").forEach((button) => {
  button.addEventListener("click", async () => {
    state.module = button.dataset.module;
    setModuleSections(state.module);
    try {
      await loadDashboard();
    } catch (error) {
      showError(error);
    }
  });
});

document.getElementById("applyCustom").addEventListener("click", async () => {
  try {
    await loadDashboard();
  } catch (error) {
    showError(error);
  }
});

setDefaultDates();
loadDashboard().catch(showError);
