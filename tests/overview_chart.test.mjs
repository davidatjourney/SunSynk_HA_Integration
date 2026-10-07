import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = { innerHTML: '', querySelector: () => null }; }
};
globalThis.window = {};
const registered = new Map();
globalThis.customElements = { get: (key) => registered.get(key), define: (key, value) => registered.set(key, value) };
const code = await readFile(new URL('../custom_components/sunsynk/www/sunsynk-overview-chart.js', import.meta.url), 'utf8');
const { validCapacity, historyPoints, linePath, SunsynkOverviewChart } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
assert.equal(registered.get('sunsynk-overview-chart'), SunsynkOverviewChart);
assert.equal(window.customCards.length, 1);
for (const invalid of [null, undefined, true, '20', 0, -1, NaN, Infinity]) assert.equal(validCapacity(invalid), false);
assert.equal(validCapacity(20), true);
const states = [{ s: '50', lu: 1 }, { s: 'unavailable', lu: 2 }, { s: '100', lu: 3 }];
const energy = historyPoints(states, 'battery_soc', '%', 20, 1000, 4000);
assert.deepEqual(energy.map(p => p.value), [10, null, 20, 20]);
assert.equal(historyPoints(states, 'battery_soc', '%', null, 1000, 4000).length, 0);
assert.equal(historyPoints([{ s: '101', lu: 1 }], 'battery_soc', '%', 20, 1000, 2000)[0].value, null);
assert.equal(historyPoints([{ s: '2000', lu: 1 }], 'pv_pac', 'W', null, 1000, 2000)[0].value, 2);
assert.equal(historyPoints([{ s: '2', lu: 1 }], 'pv_pac', 'kW', null, 1000, 2000)[0].value, 2);
assert.equal(historyPoints([{ s: '2', lu: 1 }], 'pv_pac', 'MW', null, 1000, 2000)[0].value, null);
assert.equal(historyPoints([{ s: '2', lu: 1, a: {unit_of_measurement: 'kW'} }], 'pv_pac', 'W', null, 1000, 2000)[0].value, 2);
assert.equal(historyPoints([{ state: '1000', last_updated: '1970-01-01T00:00:01Z' }], 'pv_pac', 'W', null, 1000, 2000)[0].value, 1);
assert.equal(linePath(energy, t => t / 1000, v => v), 'M1.00,10.00  M3.00,20.00 L4.00,20.00');
assert.equal(historyPoints([], 'pv_pac', 'W', null, 1000, 2000).length, 0);

let intervalCallback, cleared = false, observed = false, disconnected = false;
globalThis.setInterval = callback => { intervalCallback = callback; return 7; };
globalThis.clearInterval = id => { assert.equal(id, 7); cleared = true; };
globalThis.ResizeObserver = class { observe() { observed = true; } disconnect() { disconnected = true; } };
const card = new SunsynkOverviewChart();
card._render = () => {};
card.setConfig({ entities: { pv_pac: 'sensor.solar', load_total_power: 'sensor.load', battery_soc: 'sensor.soc' }, battery_bank_capacity_kwh: 20 });
card.isConnected = true;
let calls = 0;
card._hass = {
  states: { 'sensor.solar': { attributes: {unit_of_measurement: 'W'} }, 'sensor.load': { attributes: {unit_of_measurement: 'kW'} } },
  callWS: async message => {
    calls++;
    assert.equal(message.type, 'history/history_during_period');
    assert.deepEqual(message.entity_ids, ['sensor.solar', 'sensor.load', 'sensor.soc']);
    const now = Date.now() / 1000 - 60;
    return {'sensor.solar': [{s: '2000', lu: now}], 'sensor.load': [{s: '3', lu: now}], 'sensor.soc': [{s: '50', lu: now}]};
  },
};
await card._fetch();
assert.equal(card._series[2].points[0].value, 10);
assert.equal(card._series[0].points[0].value, 2);
assert.equal(card._end - card._start, 24 * 3600000);
card.connectedCallback();
await new Promise(resolve => setImmediate(resolve));
assert.ok(observed);
const priorCalls = calls;
intervalCallback();
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls, priorCalls + 1);
card._hass.callWS = async () => { throw new Error('offline'); };
await card._fetch();
assert.equal(card._error, true);
assert.equal(card._loading, false);
card._hass.callWS = async () => ({});
await card._fetch();
assert.equal(card._error, false);
assert.ok(card._series.every(s => s.points.length === 0));
let finish;
card._hass.callWS = () => new Promise(resolve => { finish = resolve; });
const pending = card._fetch();
card.isConnected = false;
card.disconnectedCallback();
finish({'sensor.solar': [{s: '9999', lu: Date.now() / 1000}]});
await pending;
assert.ok(cleared && disconnected);
assert.equal(card._lastFetch, 0);
assert.ok(card._series.every(s => s.points.length === 0));
card._hass.callWS = async message => { assert.deepEqual(message.entity_ids, ['sensor.solar']); return {}; };
card._config = {entities: {pv_pac: 'sensor.solar', battery_soc: 'sensor.soc'}};
card.isConnected = true;
await card._fetch();
console.log('Overview chart tests passed');
