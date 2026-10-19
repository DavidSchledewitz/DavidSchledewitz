// Claude usage: top-bar indicator for your Claude plan limits.
//
// Panel: two mini bars (5h session on top, week below) plus "62% · 51%".
// Click: a popup with each limit, its reset countdown, day ticks on weekly
// limits and an over/under-pace note. Data comes from the same endpoint as
// /usage in Claude Code, using the login Claude Code stores in
// ~/.claude/.credentials.json (read-only). It shares a cache with the
// claude-usage terminal tool, so running both doesn't double the requests.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import Pango from 'gi://Pango';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import * as U from './usage.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');
Gio._promisify(Gio.File.prototype, 'replace_contents_bytes_async', 'replace_contents_finish');
Gio._promisify(Soup.Session.prototype, 'send_and_read_async');

const API_URL = GLib.getenv('CLAUDE_USAGE_API_URL') ?? 'https://api.anthropic.com/api/oauth/usage';
const OAUTH_BETA = 'oauth-2025-04-20';
const INTERVAL = 300; // seconds between polls; the endpoint rate-limits eagerly
const MAX_BACKOFF = 30 * 60;

// GNOME palette: green 3, yellow 3, red 2
const COLORS = {
    ok: [0.20, 0.82, 0.48],
    warn: [0.96, 0.83, 0.18],
    crit: [0.93, 0.20, 0.23],
    none: [0.6, 0.6, 0.6],
};

class UsageError extends Error {
    constructor(message, retryAfter = 0) {
        super(message);
        this.retryAfter = retryAfter; // seconds the server asked us to wait
    }
}

// ---------------------------------------------------------------------------
// data

class UsageSource {
    constructor(onChange) {
        this._onChange = onChange;
        this._session = new Soup.Session({timeout: 15});
        this._cancellable = new Gio.Cancellable();
        this._timer = 0;
        this._busy = false;
        this._failures = 0;
        this.data = null;
        this.fetchedAt = null;
        this.error = null;
        this.plan = null;
    }

    start() {
        this._poll(false);
    }

    refresh() {
        this._poll(true);
    }

    destroy() {
        this._cancellable.cancel();
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._session.abort();
        this._onChange = null;
    }

    _schedule(seconds) {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, Math.max(1, Math.ceil(seconds)), () => {
            this._timer = 0;
            this._poll(false);
            return GLib.SOURCE_REMOVE;
        });
    }

    async _poll(force) {
        if (this._busy)
            return;
        this._busy = true;
        try {
            const creds = await this._loadCredentials();
            this.plan = creds.plan ?? this.plan;
            const cached = force ? null : await this._readCache();
            if (cached) {
                [this.data, this.fetchedAt] = cached;
            } else {
                this.data = await this._fetch(creds.token);
                this.fetchedAt = Date.now() / 1000;
                await this._writeCache();
            }
            this.error = null;
            this._failures = 0;
            this._schedule(this.fetchedAt + INTERVAL - Date.now() / 1000);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            this._failures++;
            const delay = Math.max(Math.min(INTERVAL * 2 ** (this._failures - 1), MAX_BACKOFF), e.retryAfter ?? 0);
            this.error = e instanceof UsageError ? e.message : `error: ${e.message}`;
            if (e.retryAfter !== undefined)
                this.error += ` Retrying in ${U.fmtDuration(delay)}.`;
            this._schedule(delay);
        } finally {
            this._busy = false;
        }
        this._onChange?.();
    }

    async _loadCredentials() {
        const envToken = GLib.getenv('CLAUDE_CODE_OAUTH_TOKEN');
        if (envToken)
            return {token: envToken, plan: null};
        const dir = GLib.getenv('CLAUDE_CONFIG_DIR') ?? GLib.build_filenamev([GLib.get_home_dir(), '.claude']);
        const file = Gio.File.new_for_path(GLib.build_filenamev([dir, '.credentials.json']));
        let blob;
        try {
            const [contents] = await file.load_contents_async(this._cancellable);
            blob = JSON.parse(new TextDecoder().decode(contents));
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
            throw new UsageError('No Claude Code login found. The desktop app\'s login can\'t be read; install Claude Code and log in once: run claude, then /login.');
        }
        const oauth = blob?.claudeAiOauth ?? {};
        if (!oauth.accessToken)
            throw new UsageError('No Claude Code login found. The desktop app\'s login can\'t be read; install Claude Code and log in once: run claude, then /login.');
        return {token: oauth.accessToken, plan: oauth.subscriptionType ?? null};
    }

    async _fetch(token) {
        const msg = Soup.Message.new('GET', API_URL);
        const headers = msg.get_request_headers();
        headers.append('Authorization', `Bearer ${token}`);
        headers.append('anthropic-beta', OAUTH_BETA);
        headers.append('Accept', 'application/json');
        headers.append('User-Agent', 'claude-usage-gnome/1');
        let bytes;
        try {
            bytes = await this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
            throw new UsageError(`Network error: ${e.message}`);
        }
        // Not get_status(): it throws for codes missing from Soup's enum, such as 429.
        const status = msg.status_code;
        if (status === 401 || status === 403)
            throw new UsageError('Login expired. Open Claude Code once so it refreshes your login.');
        if (status === 429) {
            const retryAfter = parseInt(msg.get_response_headers().get_one('Retry-After') ?? '', 10);
            throw new UsageError('Rate limited by the usage endpoint.', Number.isFinite(retryAfter) ? retryAfter : 0);
        }
        if (status !== 200)
            throw new UsageError(`HTTP ${status} from usage endpoint`);
        try {
            return JSON.parse(new TextDecoder().decode(bytes.get_data()));
        } catch {
            throw new UsageError('Usage endpoint returned invalid JSON');
        }
    }

    _cacheFile() {
        return Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_cache_dir(), 'claude-usage', 'usage.json']));
    }

    async _readCache() {
        try {
            const [contents] = await this._cacheFile().load_contents_async(this._cancellable);
            const blob = JSON.parse(new TextDecoder().decode(contents));
            if (Date.now() / 1000 - blob.fetched_at <= INTERVAL && blob.data)
                return [blob.data, blob.fetched_at];
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
        }
        return null;
    }

    async _writeCache() {
        const file = this._cacheFile();
        try {
            GLib.mkdir_with_parents(file.get_parent().get_path(), 0o755);
            const json = JSON.stringify({fetched_at: this.fetchedAt, data: this.data});
            await file.replace_contents_bytes_async(new GLib.Bytes(new TextEncoder().encode(json)),
                null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, this._cancellable);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
        }
    }
}

// ---------------------------------------------------------------------------
// drawing

function vbox(params = {}) {
    const box = new St.BoxLayout(params);
    // GNOME 48+ uses `orientation`; `vertical` is deprecated there.
    if (box.orientation !== undefined)
        box.orientation = Clutter.Orientation.VERTICAL;
    else
        box.vertical = true;
    return box;
}

function roundedRect(cr, x, y, w, h, r) {
    r = Math.min(r, h / 2, w / 2);
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

function fgColor(area) {
    const c = area.get_theme_node().get_foreground_color();
    return [c.red / 255, c.green / 255, c.blue / 255];
}

/** Track, fill, optional ticks (one per segment boundary) and a now-marker. */
function paintBar(cr, x, y, w, h, fg, {pct, elapsed = null, segments = 0, markerH = 0}) {
    const fill = pct === null ? 0 : w * Math.min(100, Math.max(0, pct)) / 100;
    cr.save();
    roundedRect(cr, x, y, w, h, h / 2);
    cr.clip();
    cr.setSourceRGBA(...fg, 0.18);
    cr.paint();
    cr.setSourceRGBA(...COLORS[U.level(pct)], 1);
    cr.rectangle(x, y, fill, h);
    cr.fill();
    cr.setLineWidth(1);
    for (let k = 1; k < segments; k++) {
        const tx = Math.round(x + k * w / segments) + 0.5;
        if (tx < x + fill)
            cr.setSourceRGBA(0, 0, 0, 0.45);
        else
            cr.setSourceRGBA(...fg, 0.35);
        cr.moveTo(tx, y);
        cr.lineTo(tx, y + h);
        cr.stroke();
    }
    cr.restore();
    if (elapsed !== null && markerH) {
        const mx = Math.round(x + elapsed * w);
        cr.setSourceRGBA(...fg, 0.95);
        cr.rectangle(Math.min(x + w - 2, Math.max(x, mx - 1)), y + h / 2 - markerH / 2, 2, markerH);
        cr.fill();
    }
}

// ---------------------------------------------------------------------------
// UI

const Indicator = GObject.registerClass(
class ClaudeUsageIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Claude usage');

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.DrawingArea({style_class: 'claude-usage-icon', y_align: Clutter.ActorAlign.CENTER});
        this._icon.connect('repaint', area => this._paintIcon(area));
        this._panelLabel = new St.Label({text: '…', y_align: Clutter.ActorAlign.CENTER, style_class: 'claude-usage-panel-label'});
        box.add_child(this._icon);
        box.add_child(this._panelLabel);
        this.add_child(box);

        this.menu.actor.add_style_class_name('claude-usage-menu');
        // Added straight to the menu box: a non-clickable menu item would be
        // styled as insensitive (greyed out) by the theme.
        this._content = vbox({style_class: 'claude-usage-popup', x_expand: true});
        this.menu.box.add_child(this._content);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const refresh = new PopupMenu.PopupMenuItem('Refresh now');
        refresh.connect('activate', () => this._source.refresh());
        this.menu.addMenuItem(refresh);

        this._clockFormat = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._rows = [];
        this._ticker = 0;
        this.menu.connect('open-state-changed', (_menu, open) => this._setTicking(open));

        this._source = new UsageSource(() => this._update());
        this._buildContent();
        this._source.start();
    }

    destroy() {
        this._setTicking(false);
        this._source?.destroy();
        this._source = null;
        super.destroy();
    }

    _setTicking(on) {
        if (on && !this._ticker) {
            this._refreshTexts();
            this._ticker = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                this._refreshTexts();
                return GLib.SOURCE_CONTINUE;
            });
        } else if (!on && this._ticker) {
            GLib.source_remove(this._ticker);
            this._ticker = 0;
        }
    }

    _limits() {
        return this._source?.data ? U.parseLimits(this._source.data) : [];
    }

    _update() {
        const limits = this._limits();
        const pct = key => limits.find(l => l.key === key)?.pct;
        const show = p => (p === null || p === undefined ? '—' : `${p.toFixed(0)}%`);
        this._panelLabel.text = this._source.data ? `${show(pct('five_hour'))} · ${show(pct('seven_day'))}` : '—';
        this._icon.queue_repaint();
        this._buildContent();
    }

    _paintIcon(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const fg = fgColor(area);
        const limits = this._limits();
        const get = key => limits.find(l => l.key === key)?.pct ?? null;
        const barH = Math.max(3, Math.floor((h - 4) / 2));
        const top = Math.floor((h - 2 * barH - 3) / 2);
        paintBar(cr, 0, top, w, barH, fg, {pct: get('five_hour')});
        paintBar(cr, 0, top + barH + 3, w, barH, fg, {pct: get('seven_day')});
        cr.$dispose();
    }

    _fmtClock(t) {
        const dt = GLib.DateTime.new_from_unix_local(Math.floor(t));
        const today = GLib.DateTime.new_now_local();
        const time = this._clockFormat.get_string('clock-format') === '12h' ? '%l:%M %p' : '%H:%M';
        const sameDay = dt.get_year() === today.get_year() && dt.get_day_of_year() === today.get_day_of_year();
        return dt.format(sameDay ? time : `%a %d %b ${time}`).trim();
    }

    _label(text, style, params = {}) {
        const label = new St.Label({text, style_class: style, ...params});
        if (style !== 'claude-usage-name' && style !== 'claude-usage-pct') {
            label.clutter_text.line_wrap = true;
            label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        }
        if (style === 'claude-usage-dim')
            label.opacity = 165; // dim relative to the theme's text color, light or dark
        return label;
    }

    _buildContent() {
        this._content.destroy_all_children();
        this._rows = [];
        this._footer = null;
        const src = this._source;

        const header = new St.BoxLayout({style_class: 'claude-usage-header'});
        header.add_child(this._label('Claude usage', 'claude-usage-title', {x_expand: true}));
        if (src.plan)
            header.add_child(this._label(`${src.plan[0].toUpperCase()}${src.plan.slice(1)} plan`, 'claude-usage-dim',
                {y_align: Clutter.ActorAlign.CENTER}));
        this._content.add_child(header);

        if (!src.data) {
            this._content.add_child(this._label(src.error ?? 'Loading…', src.error ? 'claude-usage-error' : 'claude-usage-dim'));
            return;
        }

        for (const limit of this._limits()) {
            const section = vbox({style_class: 'claude-usage-section'});
            const top = new St.BoxLayout();
            top.add_child(this._label(limit.label, 'claude-usage-name', {x_expand: true}));
            top.add_child(this._label(limit.pct === null ? '—' : `${limit.pct.toFixed(0)}%`, 'claude-usage-pct'));
            section.add_child(top);

            const days = U.windowDays(limit);
            const area = new St.DrawingArea({style_class: 'claude-usage-bar', x_expand: true});
            area.connect('repaint', a => {
                const cr = a.get_context();
                const [w, h] = a.get_surface_size();
                const now = Date.now() / 1000;
                paintBar(cr, 1, 3, w - 2, h - 6, fgColor(a), {
                    pct: limit.pct, elapsed: U.elapsedFraction(limit, now), segments: days, markerH: h,
                });
                cr.$dispose();
            });
            section.add_child(area);

            if (days && limit.resetsAt !== null) {
                const scale = new St.BoxLayout({style_class: 'claude-usage-scale', x_expand: true});
                scale.layout_manager.homogeneous = true;
                const start = limit.resetsAt - limit.window;
                // Each segment runs reset-time to reset-time; label it with the
                // day at its midpoint (the day it mostly covers), centred.
                for (let k = 0; k < days; k++) {
                    const day = GLib.DateTime.new_from_unix_local(Math.floor(start + (k + 0.5) * U.DAY)).format('%a');
                    const label = this._label(day, 'claude-usage-dim', {x_expand: true});
                    label.add_style_class_name('claude-usage-scale-day');
                    scale.add_child(label);
                }
                section.add_child(scale);
            }

            const detail = this._label('', 'claude-usage-detail');
            const pace = this._label('', 'claude-usage-dim');
            section.add_child(detail);
            section.add_child(pace);
            this._content.add_child(section);
            this._rows.push({limit, area, detail, pace});
        }

        const extra = U.parseExtra(src.data);
        if (extra !== null) {
            const section = vbox({style_class: 'claude-usage-section'});
            const top = new St.BoxLayout();
            top.add_child(this._label('Usage credits (monthly)', 'claude-usage-name', {x_expand: true}));
            top.add_child(this._label(`${extra.pct.toFixed(0)}%`, 'claude-usage-pct'));
            section.add_child(top);
            const area = new St.DrawingArea({style_class: 'claude-usage-bar', x_expand: true});
            area.connect('repaint', a => {
                const cr = a.get_context();
                const [w, h] = a.get_surface_size();
                paintBar(cr, 1, 3, w - 2, h - 6, fgColor(a), {pct: extra.pct});
                cr.$dispose();
            });
            section.add_child(area);
            if (extra.used !== null && extra.limit)
                section.add_child(this._label(`${U.fmtMoney(extra.used)} of ${U.fmtMoney(extra.limit)} spent`, 'claude-usage-detail'));
            this._content.add_child(section);
        }

        this._footer = this._label('', 'claude-usage-dim');
        this._content.add_child(this._footer);
        if (src.error)
            this._content.add_child(this._label(src.error, 'claude-usage-error'));
        this._refreshTexts();
    }

    _refreshTexts() {
        const now = Date.now() / 1000;
        for (const {limit, area, detail, pace} of this._rows) {
            const left = U.secondsLeft(limit, now);
            if (left === null)
                detail.text = limit.key === 'five_hour' ? 'No active session. It starts with your next message.' : 'No reset scheduled';
            else if (left === 0)
                detail.text = U.EXPIRING.includes(limit.key) ? 'Expired' : 'Resetting now…';
            else
                detail.text = `${U.EXPIRING.includes(limit.key) ? 'Expires' : 'Resets'} in ${U.fmtDuration(left, true)} · ${this._fmtClock(limit.resetsAt)}`;
            const note = U.paceNote(limit, now);
            pace.text = note?.text ?? '';
            pace.visible = Boolean(note);
            if (note?.over)
                pace.add_style_class_name('claude-usage-over');
            else
                pace.remove_style_class_name('claude-usage-over');
            pace.opacity = note?.over ? 255 : 165;
            area.queue_repaint();
        }
        if (this._footer && this._source?.fetchedAt)
            this._footer.text = `Updated ${U.fmtDuration(Math.max(0, now - this._source.fetchedAt))} ago`;
    }
});

export default class ClaudeUsageExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
