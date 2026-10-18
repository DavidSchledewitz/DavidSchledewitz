// Run with: gjs -m gnome-extension/tests/test_usage.js
import * as U from '../claude-usage@davidschledewitz.github.io/usage.js';
import System from 'system';

const NOW = Date.parse('2026-10-16T12:00:00Z') / 1000;
const iso = s => new Date((NOW + s) * 1000).toISOString();
const SAMPLE = {
    five_hour: {utilization: 40.0, resets_at: iso(2 * U.HOUR)},
    seven_day: {utilization: 12.0, resets_at: iso(3 * U.DAY + 4 * U.HOUR)},
    seven_day_opus: {utilization: null, resets_at: null},
    seven_day_sonnet: {utilization: 5.0, resets_at: iso(3 * U.DAY)},
    some_future_limit: {utilization: 7.0, resets_at: null},
    extra_usage: {is_enabled: true, utilization: 25.0},
};

let failed = 0, ran = 0;
function eq(name, got, want) {
    ran++;
    const [g, w] = [JSON.stringify(got), JSON.stringify(want)];
    if (g !== w) {
        failed++;
        print(`FAIL ${name}\n  got:  ${g}\n  want: ${w}`);
    }
}

const limits = U.parseLimits(SAMPLE);
eq('order + filtering', limits.map(l => l.key), ['five_hour', 'seven_day', 'seven_day_sonnet', 'some_future_limit']);
eq('unknown label', limits[3].label, 'Some future limit');
eq('five hour without session kept', U.parseLimits({five_hour: {utilization: null, resets_at: null}}).length, 1);
eq('elapsed fraction', U.elapsedFraction(limits[0], NOW).toFixed(3), '0.600');
eq('Z timestamps', U.parseLimits({five_hour: {utilization: 1, resets_at: '2026-10-16T12:00:00Z'}})[0].resetsAt, NOW);
eq('extra', U.parseExtra(SAMPLE), 25);
eq('extra disabled', U.parseExtra({extra_usage: {is_enabled: false, utilization: 3}}), null);
eq('duration h', U.fmtDuration(2 * 3600 + 13 * 60 + 5), '2h 13m');
eq('duration h s', U.fmtDuration(2 * 3600 + 13 * 60 + 5, true), '2h 13m 05s');
eq('duration d', U.fmtDuration(3 * 86400 + 4 * 3600 + 59), '3d 4h');
eq('duration s', U.fmtDuration(42), '42s');
eq('levels', [U.level(10), U.level(50), U.level(80), U.level(null)], ['ok', 'warn', 'crit', 'none']);
eq('pace under', U.paceNote(limits[0], NOW), {text: '20% under pace · ~67% at reset', over: false});
eq('pace over', U.paceNote(U.parseLimits({five_hour: {utilization: 90, resets_at: iso(2 * U.HOUR)}})[0], NOW),
    {text: '30% over pace · limit in ~20m at this rate', over: true});
eq('weekly pace', U.paceNote(limits[1], NOW), {text: 'day 4 of 7 · 43% under pace · ~22% at reset', over: false});
eq('window days', [U.windowDays(limits[0]), U.windowDays(limits[1])], [0, 7]);

print(`${ran - failed}/${ran} passed`);
System.exit(failed ? 1 : 0);
