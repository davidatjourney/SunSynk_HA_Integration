/** Bundled, dependency-free combined-system history chart. */
const SERIES = [
  { key: "pv_pac", label: "Solar", color: "#e6a000", unit: "kW" },
  { key: "load_total_power", label: "Load", color: "#2196f3", unit: "kW" },
  { key: "battery_soc", label: "Stored battery energy (estimate)", color: "#a66de0", unit: "kWh" },
];

export function validCapacity(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

export function historyPoints(history, key, unit, capacity, start, end) {
  if (key === "battery_soc" && !validCapacity(capacity)) return [];
  const points = (history || []).map((state) => {
    const raw = state.s ?? state.state;
    const stamp = state.lu ?? state.lc ?? state.last_updated ?? state.last_changed;
    const time = typeof stamp === "number" ? stamp * 1000 : Date.parse(stamp);
    let value = typeof raw === "string" && raw.trim() === "" ? NaN : Number(raw);
    if (raw == null || typeof raw === "boolean") value = NaN;
    const sampleUnit = state.a?.unit_of_measurement ?? state.attributes?.unit_of_measurement ?? unit;
    if (key === "battery_soc") {
      value = value >= 0 && value <= 100 ? value * capacity / 100 : NaN;
    } else if (sampleUnit === "W") value /= 1000;
    else if (sampleUnit !== "kW") value = NaN;
    return { time, value: Number.isFinite(value) ? value : null };
  }).filter((point) => Number.isFinite(point.time) && point.time <= end)
    .sort((a, b) => a.time - b.time);
  // Carry the recorded state at the beginning of the window, without inventing zero.
  const prior = points.filter((point) => point.time <= start).at(-1);
  const result = points.filter((point) => point.time > start);
  if (prior) result.unshift({ ...prior, time: start });
  if (result.length && result.at(-1).time < end) result.push({ ...result.at(-1), time: end });
  return result;
}

export function linePath(points, x, y) {
  let connected = false;
  return points.map((point) => {
    if (point.value === null) { connected = false; return ""; }
    const command = connected ? "L" : "M";
    connected = true;
    return `${command}${x(point.time).toFixed(2)},${y(point.value).toFixed(2)}`;
  }).join(" ");
}

export class SunsynkOverviewChart extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._width = 420;
    this._series = [];
    this._request = 0;
    this._loading = true;
  }

  setConfig(config) {
    this._config = { ...config, entities: { ...config.entities } };
    this._request++;
    this._series = [];
    this._lastFetch = 0;
    this._loading = true;
    this._error = false;
    this._render();
    if (this.isConnected && this._hass) void this._fetch();
  }

  set hass(hass) {
    this._hass = hass;
    if (this.isConnected && !this._lastFetch && this._config) void this._fetch();
  }

  connectedCallback() {
    this._observer = new ResizeObserver(([entry]) => {
      this._width = Math.max(240, entry.contentRect.width - 32);
      this._render();
    });
    this._observer.observe(this);
    this._timer = setInterval(() => void this._fetch(), 60000);
    if (this._hass && this._config) void this._fetch();
    else this._render();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._observer?.disconnect();
    this._lastFetch = 0;
    this._request++; // Discard any response arriving after removal.
  }

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: "auto", min_columns: 6 }; }

  async _fetch() {
    if (!this.isConnected || !this._hass || !this._config) return;
    const request = ++this._request;
    this._lastFetch = Date.now();
    const end = this._lastFetch;
    const start = end - 24 * 3600000;
    const capacity = this._config.battery_bank_capacity_kwh;
    const definitions = SERIES.filter((series) => this._config.entities[series.key] &&
      (series.key !== "battery_soc" || validCapacity(capacity)));
    if (!definitions.length) {
      this._series = [];
      this._loading = false;
      this._error = false;
      this._render();
      return;
    }
    try {
      const history = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(start).toISOString(),
        end_time: new Date(end).toISOString(),
        entity_ids: definitions.map((series) => this._config.entities[series.key]),
        minimal_response: false,
        no_attributes: false,
      });
      if (request !== this._request || !this.isConnected) return;
      this._start = start;
      this._end = end;
      this._series = definitions.map((series) => {
        const entity = this._config.entities[series.key];
        return { ...series, points: historyPoints(history[entity], series.key,
          this._hass.states[entity]?.attributes.unit_of_measurement, capacity, start, end) };
      });
      this._error = false;
    } catch (_error) {
      if (request !== this._request || !this.isConnected) return;
      this._error = true;
      this._series = [];
    }
    this._loading = false;
    this._render();
  }

  _render() {
    if (!this._config) return;
    const width = this._width;
    const left = 42, right = width - 44, top = 24, bottom = 254;
    const hasEnergy = validCapacity(this._config.battery_bank_capacity_kwh) && this._config.entities.battery_soc;
    const powerValues = this._series.filter((s) => s.unit === "kW").flatMap((s) => s.points)
      .filter((p) => p.value !== null).map((p) => p.value);
    const min = Math.min(0, ...powerValues);
    const max = Math.max(1, ...powerValues) * 1.05;
    const energyMax = this._config.battery_bank_capacity_kwh;
    const x = (time) => left + (time - this._start) / (this._end - this._start) * (right - left);
    const yPower = (value) => bottom - (value - min) / (max - min) * (bottom - top);
    const yEnergy = (value) => bottom - value / energyMax * (bottom - top);
    const hasData = this._series.some((s) => s.points.some((p) => p.value !== null));
    const format = (value) => Number(value.toFixed(1)).toLocaleString();
    const timeLabel = (time) => new Date(time).toLocaleTimeString(this._hass?.locale?.language, { hour: "2-digit", minute: "2-digit" });
    let graph = "";
    if (hasData) {
      let ticks = "";
      for (let i = 0; i <= 4; i++) {
        const y = bottom - i / 4 * (bottom - top);
        ticks += `<line x1="${left}" x2="${right}" y1="${y}" y2="${y}" class="grid"/>
          <text x="${left - 6}" y="${y + 4}" text-anchor="end">${format(min + i / 4 * (max - min))}</text>`;
        if (hasEnergy) ticks += `<text x="${right + 6}" y="${y + 4}">${format(i / 4 * energyMax)}</text>`;
        const time = this._start + i / 4 * (this._end - this._start);
        // Three ticks keep time labels readable at phone widths.
        if (i % 2 === 0) ticks += `<text x="${x(time)}" y="${bottom + 24}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${timeLabel(time)}</text>`;
      }
      const paths = this._series.map((s) => `<path d="${linePath(s.points, x, s.unit === "kW" ? yPower : yEnergy)}" stroke="${s.color}"/>`).join("");
      graph = `<svg viewBox="0 0 ${width} 288" role="slider" tabindex="0" aria-label="24-hour history. Use arrow keys to inspect readings." aria-valuemin="0" aria-valuemax="100" aria-valuenow="100">
        <text x="${left}" y="14">kW</text>${hasEnergy ? `<text x="${right}" y="14" text-anchor="end">kWh</text>` : ""}
        ${ticks}${paths}<line class="cursor" x1="${right}" x2="${right}" y1="${top}" y2="${bottom}" visibility="hidden"/>
        </svg>`;
    }
    this.shadowRoot.innerHTML = `<style>
      :host { display:block; min-width:0; }
      ha-card { display:block; padding:16px; box-sizing:border-box; color:var(--primary-text-color); background:var(--ha-card-background,var(--card-background-color)); }
      h2 { font-size:18px; font-weight:500; margin:0 0 8px; }
      .subtitle,.status,.hint { color:var(--secondary-text-color); font-size:13px; overflow-wrap:anywhere; }
      svg { display:block; width:100%; height:auto; touch-action:pan-y; }
      svg:focus-visible { outline:2px solid var(--primary-color); }
      text { fill:var(--secondary-text-color); font-size:12px; }
      path { fill:none; stroke-width:2; stroke-linejoin:round; }
      .grid { stroke:var(--divider-color,#888); opacity:.35; }
      .cursor { stroke:var(--primary-text-color); stroke-dasharray:4 4; }
      .legend { display:flex; flex-wrap:wrap; gap:8px 16px; font-size:13px; margin:12px 0; }
      .legend span { display:flex; align-items:center; gap:6px; }
      .swatch { width:14px; height:3px; flex-shrink:0; }
      .tooltip { min-height:64px; font-size:13px; line-height:1.5; overflow-wrap:anywhere; }
      button { min-height:44px; background:transparent; color:var(--primary-color); border:1px solid var(--divider-color); border-radius:8px; padding:0 16px; cursor:pointer; }
    </style><ha-card><h2>Solar, load &amp; battery</h2><div class="subtitle">Last 24 hours</div>
      ${this._loading ? '<p class="status">Loading history…</p>' : ""}
      ${this._error ? '<p class="status">Could not load history. Check Home Assistant history availability.</p><button>Retry</button>' : ""}
      ${!this._loading && !this._error && !hasData ? '<p class="status">No recorded combined readings are available for this period.</p>' : ""}
      ${graph}<div class="legend">${this._series.map((s) => `<span><i class="swatch" style="background:${s.color}"></i>${s.label} (${s.unit})</span>`).join("")}</div>
      ${hasData ? '<div class="tooltip" role="status" aria-live="polite">Touch or hover over the graph to inspect readings.</div>' : ""}
      ${!validCapacity(this._config.battery_bank_capacity_kwh) ? '<p class="hint">Set Total battery-bank capacity (kWh) in Sunsynk → Configure to show estimated stored battery energy.</p>' : ""}
    </ha-card>`;
    this.shadowRoot.querySelector("button")?.addEventListener("click", () => void this._fetch());
    const svg = this.shadowRoot.querySelector("svg");
    if (!svg) return;
    const inspect = (fraction) => {
      this._fraction = Math.max(0, Math.min(1, fraction));
      const time = this._start + this._fraction * (this._end - this._start);
      const line = svg.querySelector(".cursor");
      line.setAttribute("x1", x(time)); line.setAttribute("x2", x(time)); line.setAttribute("visibility", "visible");
      const tooltip = this.shadowRoot.querySelector(".tooltip");
      tooltip.textContent = new Date(time).toLocaleString(this._hass?.locale?.language) + " — " + this._series.map((s) => {
        const point = s.points.filter((p) => p.time <= time).at(-1);
        return `${s.label}: ${point?.value != null ? point.value.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + s.unit : "unavailable"}`;
      }).join(" · ");
      svg.setAttribute("aria-valuenow", Math.round(this._fraction * 100));
      svg.setAttribute("aria-valuetext", tooltip.textContent);
    };
    const pointer = (event) => {
      const rect = svg.getBoundingClientRect();
      inspect(((event.clientX - rect.left) / rect.width * width - left) / (right - left));
    };
    svg.addEventListener("pointermove", pointer);
    svg.addEventListener("pointerdown", pointer);
    svg.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      inspect(event.key === "Home" ? 0 : event.key === "End" ? 1 : (this._fraction ?? 1) + (event.key === "ArrowLeft" ? -1 : 1) / 48);
    });
  }
}

if (!customElements.get("sunsynk-overview-chart")) customElements.define("sunsynk-overview-chart", SunsynkOverviewChart);
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "sunsynk-overview-chart")) {
  window.customCards.push({ type: "sunsynk-overview-chart", name: "Sunsynk overview history", description: "Solar/load power and estimated stored battery energy over 24 hours." });
}
