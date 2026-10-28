// Claude usage: top-bar indicator for your Claude plan limits.
//
// An expired login is renewed the way Claude Code renews it and written back
// to Claude Code's credentials file, so a browser login is rarely needed.
// Creating ~/.config/claude-usage/no-auto-renew turns that off.
//
// Panel: two mini bars (5h session on top, week below) plus "62% · 51%".
// Click: a popup with each limit, its reset countdown, day ticks on weekly
// limits and an over/under-pace note. Data comes from the same endpoint as
// /usage in Claude Code, using the login Claude Code stores in
// ~/.claude/.credentials.json (read-only). It shares a cache with the
// claude-usage terminal tool, so running both doesn't double the requests.
// "Usage Report" runs `claude-usage report` (installed alongside)
// and opens its page in the browser.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import Pango from 'gi://Pango';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import * as U from './usage.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');
Gio._promisify(Gio.File.prototype, 'replace_contents_bytes_async', 'replace_contents_finish');
Gio._promisify(Gio.File.prototype, 'append_to_async');
Gio._promisify(Gio.OutputStream.prototype, 'write_bytes_async');
Gio._promisify(Gio.OutputStream.prototype, 'close_async');
Gio._promisify(Soup.Session.prototype, 'send_and_read_async');

function expired(creds) {
    return creds.expiresAt !== null && Date.now() / 1000 >= creds.expiresAt / 1000 - RENEW_MARGIN;
}

const API_URL = GLib.getenv('CLAUDE_USAGE_API_URL') ?? 'https://api.anthropic.com/api/oauth/usage';
const OAUTH_BETA = 'oauth-2025-04-20';
const TOKEN_URL = GLib.getenv('CLAUDE_USAGE_TOKEN_URL') ?? 'https://platform.claude.com/v1/oauth/token';
// Claude Code's OAuth client. Renewing exactly as Claude Code does keeps the
// renewed login valid for Claude Code as well.
const CLAUDE_CODE_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
const RENEW_MARGIN = 60; // renew this many seconds before the login expires
const LOCK_STALE = 60; // seconds after which a leftover renew lock is ignored
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
    constructor(message, {retryAfter = 0, loginNeeded = false, rejected = false} = {}) {
        super(message);
        this.retryAfter = retryAfter; // seconds the server asked us to wait
        this.loginNeeded = loginNeeded; // only a new (browser) login helps
        this.rejected = rejected; // the usage endpoint refused the login (401/403)
    }
}

const NO_LOGIN = 'No Claude Code login found. The desktop app\'s login can\'t be read by other programs.';
const LOGIN_TIMEOUT = 10 * 60; // seconds to wait for the browser login
const REPORT_LABEL = 'Usage Report';

/** Claude Code's command. The desktop session's PATH may lack ~/.local/bin. */
function findClaude() {
    const found = GLib.find_program_in_path('claude');
    if (found)
        return found;
    const home = GLib.get_home_dir();
    for (const path of [`${home}/.local/bin/claude`, `${home}/.claude/local/claude`]) {
        if (GLib.file_test(path, GLib.FileTest.IS_EXECUTABLE))
            return path;
    }
    return null;
}

/**
 * Command for `claude-usage report`: the copy installed with this extension
 * (same version as the extension), else the claude-usage command.
 */
function reportCommand(extPath) {
    const python = GLib.find_program_in_path('python3');
    const script = GLib.build_filenamev([extPath, 'claude_usage.py']);
    if (python && GLib.file_test(script, GLib.FileTest.EXISTS))
        return [python, script, 'report'];
    const cmd = GLib.find_program_in_path('claude-usage') ??
        [`${GLib.get_home_dir()}/.local/bin/claude-usage`].find(p => GLib.file_test(p, GLib.FileTest.IS_EXECUTABLE));
    return cmd ? [cmd, 'report'] : null;
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
        this.loginNeeded = false;
        this.loggingIn = false;
        this._loginProc = null;
        this._loginTimer = 0;
    }

    start() {
        this._poll(false);
    }

    refresh() {
        this._poll(true);
    }

    /** Run Claude Code's own browser login, then refresh. */
    login() {
        if (this._loginProc)
            return;
        const claude = findClaude();
        if (!claude) {
            this.error = 'Claude Code isn\'t installed. See the claude-usage README for the one-line install.';
            this._onChange?.();
            return;
        }
        try {
            // stdin stays an open pipe: Claude Code offers a paste-the-code
            // fallback there, but the browser redirect normally finishes it.
            this._loginProc = Gio.Subprocess.new([claude, 'auth', 'login', '--claudeai'],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (e) {
            this.error = `Couldn't start the login: ${e.message}`;
            this._onChange?.();
            return;
        }
        this.loggingIn = true;
        this._onChange?.();
        const proc = this._loginProc;
        this._loginTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, LOGIN_TIMEOUT, () => {
            this._loginTimer = 0;
            proc.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        proc.wait_check_async(this._cancellable, (p, res) => {
            let ok = false;
            try {
                ok = p.wait_check_finish(res);
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
            }
            if (this._loginTimer)
                GLib.source_remove(this._loginTimer);
            this._loginTimer = 0;
            this._loginProc = null;
            this.loggingIn = false;
            if (ok) {
                this._failures = 0;
                this.refresh();
            } else {
                this.error = 'The login didn\'t complete. Try again with “Log in to Claude…”.';
                this._onChange?.();
            }
        });
    }

    destroy() {
        this._cancellable.cancel();
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        if (this._loginTimer)
            GLib.source_remove(this._loginTimer);
        this._loginTimer = 0;
        this._loginProc?.force_exit();
        this._loginProc = null;
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
                this.data = await this._fetchRenewing(creds);
                this.fetchedAt = Date.now() / 1000;
                await this._writeCache();
                await this._appendHistory();
            }
            this.error = null;
            this.loginNeeded = false;
            this._failures = 0;
            this._schedule(this.fetchedAt + INTERVAL - Date.now() / 1000);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            this._failures++;
            this.loginNeeded = Boolean(e.loginNeeded);
            const delay = Math.max(Math.min(INTERVAL * 2 ** (this._failures - 1), MAX_BACKOFF), e.retryAfter ?? 0);
            this.error = e instanceof UsageError ? e.message : `error: ${e.message}`;
            if (e instanceof UsageError && !e.loginNeeded)
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
            return {token: envToken, plan: null, refreshToken: null, scopes: [], expiresAt: null, file: null};
        const dir = GLib.getenv('CLAUDE_CONFIG_DIR') ?? GLib.build_filenamev([GLib.get_home_dir(), '.claude']);
        const file = Gio.File.new_for_path(GLib.build_filenamev([dir, '.credentials.json']));
        let blob;
        try {
            const [contents] = await file.load_contents_async(this._cancellable);
            blob = JSON.parse(new TextDecoder().decode(contents));
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
            throw new UsageError(NO_LOGIN, {loginNeeded: true});
        }
        const oauth = blob?.claudeAiOauth ?? {};
        if (!oauth.accessToken)
            throw new UsageError(NO_LOGIN, {loginNeeded: true});
        return {
            token: oauth.accessToken,
            plan: oauth.subscriptionType ?? null,
            refreshToken: oauth.refreshToken || null,
            scopes: Array.isArray(oauth.scopes) ? oauth.scopes : [],
            expiresAt: typeof oauth.expiresAt === 'number' ? oauth.expiresAt : null, // ms
            file,
        };
    }

    /** Fetch usage, renewing the login first if it expired, and once more if
     * Claude rejects it anyway. */
    async _fetchRenewing(creds) {
        creds = await this._renew(creds, false);
        try {
            return await this._fetch(creds.token);
        } catch (e) {
            if (!e.rejected)
                throw e;
            const renewed = await this._renew(creds, true);
            if (renewed.token === creds.token)
                throw e;
            return this._fetch(renewed.token);
        }
    }

    _renewalDisabled() {
        return GLib.file_test(GLib.build_filenamev([GLib.get_user_config_dir(), 'claude-usage', 'no-auto-renew']),
            GLib.FileTest.EXISTS);
    }

    /** Only one program (top bar or terminal app) renews at a time, since each
     * renewal replaces the refresh token. Returns the lock file, or null. */
    _takeLock() {
        const lock = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_cache_dir(), 'claude-usage', 'renew.lock']));
        GLib.mkdir_with_parents(lock.get_parent().get_path(), 0o755);
        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                lock.create(Gio.FileCreateFlags.PRIVATE, null).close(null);
                return lock;
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
                    return null;
            }
            try {
                const info = lock.query_info('time::modified', Gio.FileQueryInfoFlags.NONE, null);
                if (Date.now() / 1000 - info.get_modification_date_time().to_unix() <= LOCK_STALE)
                    return null; // the other program is renewing right now
                lock.delete(null); // left behind by a crash
            } catch {
                // vanished meanwhile: just try again
            }
        }
        return null;
    }

    /** Renew an expired login with its refresh token, as Claude Code does.
     * With force, renew although it hasn't expired (Claude rejected it). */
    async _renew(creds, force) {
        if (!creds.file || !creds.refreshToken || this._renewalDisabled())
            return creds;
        if (!force && !expired(creds))
            return creds;
        const lock = this._takeLock();
        if (!lock)
            return creds;
        try {
            const current = await this._loadCredentials();
            if (current.token !== creds.token || (!force && !expired(current)))
                return current; // renewed meanwhile, by Claude Code or the terminal app
            const body = {grant_type: 'refresh_token', refresh_token: current.refreshToken, client_id: CLAUDE_CODE_CLIENT_ID};
            if (current.scopes.length)
                body.scope = current.scopes.join(' ');
            const msg = Soup.Message.new('POST', TOKEN_URL);
            msg.get_request_headers().append('User-Agent', 'claude-usage-gnome/1');
            msg.set_request_body_from_bytes('application/json', new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body))));
            let bytes;
            try {
                bytes = await this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable);
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    throw e;
                throw new UsageError(`Network error while renewing the login: ${e.message}`);
            }
            const status = msg.status_code;
            if (status === 400 || status === 401 || status === 403)
                throw new UsageError('Your Claude login has expired.', {loginNeeded: true});
            if (status !== 200)
                throw new UsageError(`HTTP ${status} while renewing the login`);
            let reply;
            try {
                reply = JSON.parse(new TextDecoder().decode(bytes.get_data()));
            } catch {
                throw new UsageError('Renewing the login returned invalid JSON');
            }
            if (!reply?.access_token)
                throw new UsageError('Renewing the login returned no token');
            await this._saveRenewed(current.file, reply);
            return await this._loadCredentials();
        } finally {
            try {
                lock.delete(null);
            } catch {}
        }
    }

    /** Store a renewed login the way Claude Code does, keeping every other
     * field. Gio writes a private temp file and renames it into place, so
     * Claude Code never reads a half-written file. */
    async _saveRenewed(file, reply) {
        const [contents] = await file.load_contents_async(this._cancellable);
        const blob = JSON.parse(new TextDecoder().decode(contents));
        const oauth = blob.claudeAiOauth ??= {};
        oauth.accessToken = reply.access_token;
        if (reply.refresh_token)
            oauth.refreshToken = reply.refresh_token;
        if (typeof reply.expires_in === 'number')
            oauth.expiresAt = Math.floor(Date.now() + reply.expires_in * 1000);
        if (typeof reply.scope === 'string' && reply.scope.trim())
            oauth.scopes = reply.scope.trim().split(/\s+/);
        await file.replace_contents_bytes_async(new GLib.Bytes(new TextEncoder().encode(JSON.stringify(blob))),
            null, false, Gio.FileCreateFlags.REPLACE_DESTINATION | Gio.FileCreateFlags.PRIVATE, this._cancellable);
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
            throw new UsageError('Your Claude login has expired.', {loginNeeded: true, rejected: true});
        if (status === 429) {
            const retryAfter = parseInt(msg.get_response_headers().get_one('Retry-After') ?? '', 10);
            throw new UsageError('Rate limited by the usage endpoint.', {retryAfter: Number.isFinite(retryAfter) ? retryAfter : 0});
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

    async _appendHistory() {
        const line = U.historyLine(this.data, this.fetchedAt);
        if (line === null)
            return;
        const file = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_data_dir(), 'claude-usage', 'history.jsonl']));
        try {
            GLib.mkdir_with_parents(file.get_parent().get_path(), 0o755);
            const stream = await file.append_to_async(Gio.FileCreateFlags.NONE, GLib.PRIORITY_DEFAULT, this._cancellable);
            // One short write, so the terminal app and this never interleave lines.
            await stream.write_bytes_async(new GLib.Bytes(new TextEncoder().encode(`${line}\n`)),
                GLib.PRIORITY_DEFAULT, this._cancellable);
            await stream.close_async(GLib.PRIORITY_DEFAULT, this._cancellable);
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
    _init(extPath) {
        super._init(0.5, 'Claude usage');
        this._extPath = extPath;

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
        this._loginItem = new PopupMenu.PopupMenuItem('Log in to Claude… (opens your browser)');
        this._loginItem.connect('activate', () => this._source.login());
        this._loginItem.visible = false;
        this.menu.addMenuItem(this._loginItem);
        this._reportItem = new PopupMenu.PopupMenuItem(REPORT_LABEL);
        this._reportItem.connect('activate', () => this._openReport());
        this.menu.addMenuItem(this._reportItem);
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
        this._notifSource?.destroy();
        this._notifSource = null;
        super.destroy();
    }

    _login() {
        if (this._source?.loginNeeded)
            this._source.login();
    }

    /** Build the usage report in the background; it opens in the browser. */
    _openReport() {
        if (this._reportProc)
            return;
        const cmd = reportCommand(this._extPath);
        if (!cmd) {
            this._notify('Couldn\'t build the usage report', 'It needs Python 3. Install it with: sudo apt install python3');
            return;
        }
        let proc;
        try {
            proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            this._notify('Couldn\'t build the usage report', e.message);
            return;
        }
        this._reportProc = proc;
        this._reportItem.label.text = 'Building the report…';
        this._reportItem.setSensitive(false);
        proc.communicate_utf8_async(null, null, (p, res) => {
            let stderr = '';
            try {
                [, , stderr] = p.communicate_utf8_finish(res);
            } catch (e) {
                stderr = e.message;
            }
            this._reportProc = null;
            if (!this._source)
                return; // disabled meanwhile
            this._reportItem.label.text = REPORT_LABEL;
            this._reportItem.setSensitive(true);
            if (!p.get_successful()) {
                const lines = (stderr ?? '').trim().split('\n');
                this._notify('Couldn\'t build the usage report', lines[lines.length - 1].replace(/^claude-usage report: /, ''));
            }
        });
    }

    _notifSourceOrNew() {
        if (!this._notifSource) {
            this._notifSource = new MessageTray.Source({title: 'Claude usage', iconName: 'dialog-password-symbolic'});
            this._notifSource.connect('destroy', () => {
                this._notifSource = null;
            });
            Main.messageTray.add(this._notifSource);
        }
        return this._notifSource;
    }

    _notify(title, body) {
        const source = this._notifSourceOrNew();
        source.addNotification(new MessageTray.Notification({source, title, body}));
    }

    /** One notification per expired login, with a button for the browser login. */
    _notifyLogin() {
        if (this._loginNotified)
            return;
        this._loginNotified = true;
        this._notifSourceOrNew();
        const notification = new MessageTray.Notification({
            source: this._notifSource,
            title: 'Claude login expired',
            body: 'Click to log in again (opens your browser).',
            urgency: MessageTray.Urgency.HIGH,
        });
        notification.addAction('Log in', () => this._login());
        notification.connect('activated', () => this._login());
        notification.connect('destroy', () => {
            if (this._loginNotification === notification)
                this._loginNotification = null;
        });
        this._loginNotification = notification;
        this._notifSource.addNotification(notification);
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
        const src = this._source;
        if (src.loggingIn)
            this._panelLabel.text = 'logging in…';
        else if (src.loginNeeded)
            this._panelLabel.text = '⚠ log in'; // the numbers would be stale
        else
            this._panelLabel.text = src.data ? `${show(pct('five_hour'))} · ${show(pct('seven_day'))}` : '—';

        if (src.loginNeeded && !src.loggingIn) {
            this._notifyLogin();
        } else if (!src.loginNeeded) {
            this._loginNotified = false;
            this._loginNotification?.destroy();
        }
        this._icon.queue_repaint();
        this._buildContent();
    }

    _paintIcon(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const fg = fgColor(area);
        const limits = this._limits();
        const stale = this._source?.loginNeeded; // grey bars while the numbers can't update
        const get = key => (stale ? null : limits.find(l => l.key === key)?.pct ?? null);
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

        this._loginItem.visible = src.loginNeeded && !src.loggingIn;

        if (!src.data) {
            if (src.loggingIn)
                this._content.add_child(this._label('Waiting for you to log in in the browser…', 'claude-usage-detail'));
            else
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
        if (src.loggingIn)
            this._content.add_child(this._label('Waiting for you to log in in the browser…', 'claude-usage-detail'));
        else if (src.error)
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
        this._indicator = new Indicator(this.path);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
