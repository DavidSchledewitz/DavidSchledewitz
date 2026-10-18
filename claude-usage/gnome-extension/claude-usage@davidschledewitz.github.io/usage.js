// Parsing and formatting for the usage endpoint. No Shell imports, so this
// file can be unit-tested with plain gjs. Mirrors claude_usage.py.

export const HOUR = 3600;
export const DAY = 24 * HOUR;

// key -> [label, window length in seconds]
export const KNOWN_LIMITS = {
    five_hour: ['Current session (5h)', 5 * HOUR],
    seven_day: ['Week · all models', 7 * DAY],
    seven_day_opus: ['Week · Opus', 7 * DAY],
    seven_day_sonnet: ['Week · Sonnet', 7 * DAY],
    seven_day_oauth_apps: ['Week · OAuth apps', 7 * DAY],
};
const ALWAYS_SHOWN = ['five_hour', 'seven_day'];

function guess(key) {
    if (key in KNOWN_LIMITS)
        return KNOWN_LIMITS[key];
    const label = key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
    if (key.startsWith('five_hour'))
        return [label, 5 * HOUR];
    if (key.startsWith('seven_day'))
        return [label, 7 * DAY];
    return [label, null];
}

function parseTime(value) {
    if (!value || typeof value !== 'string')
        return null;
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? null : ms / 1000;
}

/**
 * Any object carrying a `utilization` field is a limit, so new limit types
 * show up without a code change. Times are unix seconds.
 */
export function parseLimits(data) {
    const order = Object.keys(KNOWN_LIMITS);
    const limits = [];
    for (const [key, val] of Object.entries(data ?? {})) {
        if (!val || typeof val !== 'object' || !('utilization' in val) || key === 'extra_usage')
            continue;
        if (val.utilization === null && !ALWAYS_SHOWN.includes(key))
            continue;
        const [label, window] = guess(key);
        limits.push({
            key, label, window,
            pct: val.utilization === null ? null : Number(val.utilization),
            resetsAt: parseTime(val.resets_at),
        });
    }
    const rank = l => (order.includes(l.key) ? order.indexOf(l.key) : order.length);
    return limits.sort((a, b) => rank(a) - rank(b));
}

export function parseExtra(data) {
    const extra = data?.extra_usage;
    if (extra && extra.is_enabled && extra.utilization !== null && extra.utilization !== undefined)
        return Number(extra.utilization);
    return null;
}

export function secondsLeft(limit, now) {
    return limit.resetsAt === null ? null : Math.max(0, limit.resetsAt - now);
}

export function elapsedFraction(limit, now) {
    const left = secondsLeft(limit, now);
    if (left === null || !limit.window)
        return null;
    return Math.min(1, Math.max(0, 1 - left / limit.window));
}

/** Number of days in a multi-day window (7 for weekly), else 0. */
export function windowDays(limit) {
    return limit.window && limit.window >= 2 * DAY ? Math.floor(limit.window / DAY) : 0;
}

export function fmtDuration(seconds, withSeconds = false) {
    let s = Math.floor(seconds);
    const d = Math.floor(s / DAY);
    s -= d * DAY;
    const h = Math.floor(s / HOUR);
    s -= h * HOUR;
    const m = Math.floor(s / 60);
    s -= m * 60;
    const pad = n => String(n).padStart(2, '0');
    if (d)
        return `${d}d ${h}h`;
    if (h)
        return withSeconds ? `${h}h ${pad(m)}m ${pad(s)}s` : `${h}h ${pad(m)}m`;
    if (m)
        return withSeconds ? `${m}m ${pad(s)}s` : `${m}m`;
    return `${s}s`;
}

/** 'ok' below 50%, 'warn' below 80%, else 'crit'. */
export function level(pct) {
    if (pct === null)
        return 'none';
    return pct >= 80 ? 'crit' : pct >= 50 ? 'warn' : 'ok';
}

/**
 * How you compare with an even burn across the window, plus a linear
 * projection. Returns {text, over} or null.
 */
export function paceNote(limit, now) {
    const left = secondsLeft(limit, now);
    if (limit.pct === null || left === null || !limit.window)
        return null;
    if (limit.pct >= 100)
        return {text: 'limit reached', over: true};
    const elapsed = limit.window - left;
    const parts = [];
    const days = windowDays(limit);
    if (days)
        parts.push(`day ${Math.min(days, Math.floor(elapsed / DAY) + 1)} of ${days}`);
    let over = false;
    if (elapsed >= limit.window * 0.05) {
        const diff = limit.pct - 100 * elapsed / limit.window;
        if (Math.abs(diff) < 1)
            parts.push('on pace');
        else
            parts.push(`${Math.abs(diff).toFixed(0)}% ${diff > 0 ? 'over' : 'under'} pace`);
        if (limit.pct > 0) {
            const rate = limit.pct / elapsed;
            const toFull = (100 - limit.pct) / rate;
            if (toFull < left)
                parts.push(`limit in ~${fmtDuration(toFull)} at this rate`);
            else
                parts.push(`~${(limit.pct + rate * left).toFixed(0)}% at reset`);
        }
        over = diff >= 1;
    }
    return parts.length ? {text: parts.join(' · '), over} : null;
}
