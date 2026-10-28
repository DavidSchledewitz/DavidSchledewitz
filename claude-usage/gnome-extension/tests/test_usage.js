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
    iguana_necktie: {utilization: 11.0, resets_at: iso(17 * U.DAY + 16 * U.HOUR)},
    extra_usage: {is_enabled: true, monthly_limit: 4000, used_credits: 147.0, utilization: 3.675},
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
eq('order + filtering', limits.map(l => l.key), ['five_hour', 'seven_day', 'seven_day_sonnet', 'iguana_necktie', 'some_future_limit']);
eq('unknown label', limits[4].label, 'Some future limit');
eq('cloud credits label', limits[3].label, 'Cloud session credits');
eq('cloud credits no pace', U.paceNote(limits[3], NOW), null);
eq('five hour without session kept', U.parseLimits({five_hour: {utilization: null, resets_at: null}}).length, 1);
eq('elapsed fraction', U.elapsedFraction(limits[0], NOW).toFixed(3), '0.600');
eq('Z timestamps', U.parseLimits({five_hour: {utilization: 1, resets_at: '2026-10-16T12:00:00Z'}})[0].resetsAt, NOW);
eq('extra', U.parseExtra(SAMPLE), {pct: 3.675, used: 147, limit: 4000});
eq('extra derived pct', U.parseExtra({extra_usage: {is_enabled: true, monthly_limit: 100000, used_credits: 0, utilization: null}}).pct, 0);
eq('money', U.fmtMoney(147), '$1.47');
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
eq('cloud credits shown while left', U.finished(limits[3], NOW), false);
eq('cloud credits used up', U.finished({...limits[3], pct: 100}, NOW), true);
eq('cloud credits expired', U.finished({...limits[3], resetsAt: NOW - 1}, NOW), true);
eq('weekly limit at 100% stays', U.finished({...limits[1], pct: 100}, NOW), false);
eq('window days', [U.windowDays(limits[0]), U.windowDays(limits[1])], [0, 7]);
// Same fields as history_line in claude_usage.py, which claude_usage_report.py reads.
eq('history line', U.historyLine({five_hour: {utilization: 40, resets_at: '2026-10-16T14:00:00Z'},
    seven_day_opus: {utilization: null}, extra_usage: {is_enabled: true, utilization: 3}}, 1760616000.04),
'{"t":1760616000,"limits":{"five_hour":{"pct":40,"resets_at":"2026-10-16T14:00:00Z"},"seven_day_opus":{"pct":null,"resets_at":null}}}');
eq('history line without limits', U.historyLine({extra_usage: {is_enabled: false}}, 1), null);

print(`${ran - failed}/${ran} passed`);
System.exit(failed ? 1 : 0);
