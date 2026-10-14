// Session Pulse for GNOME Shell: shows the state written by `sessionpulse daemon`.
//
// It only reads ~/.local/share/sessionpulse/status.json (format: docs/status-json.md)
// and, when you open the dashboard, the token next to it. No network, no subprocesses.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const SCHEMA = 1;
const SLOTS = 8; // fixed rows for open sessions, so an open menu never rebuilds under the pointer
const POLL_SECONDS = 5;
const STALE_INTERVALS = 3;
const SETUP_URL = 'https://github.com/vijayshuklaHL/sessionpulse#gnome-extension';

function short(s, n = 46) {
    return s.length <= n ? s : `${s.slice(0, n - 1)}…`;
}

function fmtDuration(ms) {
    const m = Math.max(0, Math.floor(ms / 60000));
    return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

function fmtHours(h) {
    return h >= 1 ? `${h.toFixed(1)}h` : `${Math.round(h * 60)}m`;
}

function fmtCount(n) {
    if (n >= 1e6)
        return `${(n / 1e6).toFixed(1)}M`;
    return n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
}

const Indicator = GObject.registerClass(
class SessionPulseIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'Session Pulse');

        const dataDir = GLib.build_filenamev([GLib.get_user_data_dir(), 'sessionpulse']);
        this._statusFile = Gio.File.new_for_path(GLib.build_filenamev([dataDir, 'status.json']));
        this._tokenFile = Gio.File.new_for_path(GLib.build_filenamev([dataDir, 'token']));
        this._cancellable = new Gio.Cancellable();
        this._status = null;

        const iconDir = ext.dir.get_child('icons');
        this._icons = {};
        for (const state of ['busy', 'idle', 'none'])
            this._icons[state] = new Gio.FileIcon({file: iconDir.get_child(`sessionpulse-${state}.svg`)});

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({gicon: this._icons.none, style_class: 'system-status-icon'});
        this._count = new St.Label({
            text: '',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'sessionpulse-count',
            visible: false,
        });
        box.add_child(this._icon);
        box.add_child(this._count);
        this.add_child(box);

        this._buildMenu();

        // The daemon replaces status.json by rename, so watch the folder; poll as well,
        // because the folder may not exist yet and staleness needs a clock anyway.
        try {
            this._monitor = Gio.File.new_for_path(dataDir).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitor.connect('changed', (_m, file, other) => {
                if (file?.get_basename() === 'status.json' || other?.get_basename() === 'status.json')
                    this._refresh();
            });
        } catch (e) {
            this._monitor = null;
        }
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_SECONDS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._refresh();
        });

        this._refresh();
    }

    _item(label, onActivate = null, muted = !onActivate) {
        const item = new PopupMenu.PopupMenuItem(label, {reactive: Boolean(onActivate)});
        if (onActivate)
            item.connect('activate', onActivate);
        if (muted)
            item.label.add_style_class_name('sessionpulse-muted');
        this.menu.addMenuItem(item);
        return item;
    }

    _buildMenu() {
        const openDashboard = () => this._openDashboard('');
        const openBoard = () => this._openDashboard('#board');

        this._header = this._item('', null, false);
        this._hint = this._item('Start it with: sessionpulse daemon');
        this._setup = this._item('How to set it up…', () => this._launch(SETUP_URL));
        this._sessionsSep = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._sessionsSep);
        this._rows = [];
        for (let i = 0; i < SLOTS; i++)
            this._rows.push(this._item('', openDashboard));
        this._more = this._item('', openDashboard);
        this._todaySep = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._todaySep);
        this._today = this._item('');
        this._warnings = this._item('', openDashboard);
        this._actionsSep = new PopupMenu.PopupSeparatorMenuItem();
        this.menu.addMenuItem(this._actionsSep);
        this._dashboard = this._item('Open dashboard', openDashboard);
        this._board = this._item('Open to-do board', openBoard);
    }

    _refresh() {
        this._statusFile.load_contents_async(this._cancellable, (file, res) => {
            let status = null;
            try {
                const [, bytes] = file.load_contents_finish(res);
                status = JSON.parse(new TextDecoder().decode(bytes));
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                status = null; // missing, or caught mid-write by an older writer
            }
            this._apply(status);
        });
    }

    _apply(status) {
        const now = Date.now();
        let problem = null;
        if (!status)
            problem = "Session Pulse isn't running";
        else if (typeof status.schema !== 'number' || status.schema > SCHEMA)
            problem = 'Update the Session Pulse extension';
        else if (now - status.updated > (status.interval || POLL_SECONDS) * 1000 * STALE_INTERVALS)
            problem = 'Session Pulse stopped updating';
        this._status = problem ? null : status;

        this._header.label.text = problem ?? (status.open
            ? `${status.open} open · ${status.working} working · ${status.open - status.working} idle`
            : 'No Claude Code sessions running');
        this._hint.label.text = problem === 'Session Pulse stopped updating'
            ? 'Restart it with: sessionpulse daemon'
            : 'Start it with: sessionpulse daemon';
        this._hint.visible = problem !== null && problem !== 'Update the Session Pulse extension';
        this._setup.visible = problem !== null;

        const sessions = problem ? [] : status.sessions ?? [];
        this._rows.forEach((row, i) => {
            const s = sessions[i];
            row.visible = Boolean(s);
            if (s) {
                const working = s.state === 'busy';
                row.label.text = `${working ? '●' : '○'}  ${short(s.project)}  —  ` +
                    `${working ? 'working' : 'idle'} · ${fmtDuration(now - (s.started || now))}`;
            }
        });
        const extra = sessions.length - SLOTS;
        this._more.visible = extra > 0;
        this._more.label.text = `+${extra} more…`;
        this._sessionsSep.visible = sessions.length > 0;

        const today = problem ? null : status.today;
        this._today.visible = Boolean(today);
        if (today) {
            this._today.label.text = `Today: ${today.sessions} sessions · ${fmtHours(today.active_hours)} active · ` +
                `${fmtCount(today.output_tokens)} output tokens`;
        }
        this._warnings.visible = !problem && status.warnings > 0;
        if (this._warnings.visible)
            this._warnings.label.text = `⚠ ${status.warnings} data warning(s): see dashboard`;
        this._todaySep.visible = !problem;
        this._actionsSep.visible = !problem;
        this._dashboard.visible = !problem;
        this._board.visible = !problem;
        const todos = problem ? 0 : status.todos_open ?? 0;
        this._board.label.text = todos ? `Open to-do board (${todos} open)` : 'Open to-do board';

        const state = problem ? 'none' : status.state;
        this._icon.gicon = this._icons[state] ?? this._icons.none;
        this._count.text = problem ? '' : String(status.open || '');
        this._count.visible = !problem && status.open > 0;
        this.accessible_name = problem ?? `${status.open} Claude Code sessions, ${status.working} working`;
    }

    _openDashboard(fragment) {
        const port = this._status?.port;
        if (!port)
            return;
        this._tokenFile.load_contents_async(this._cancellable, (file, res) => {
            let token = '';
            try {
                const [, bytes] = file.load_contents_finish(res);
                token = new TextDecoder().decode(bytes).trim();
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
            }
            // The token signs this browser in once (it is then kept as a cookie), as `sessionpulse open` does.
            const query = token ? `?token=${encodeURIComponent(token)}` : '';
            this._launch(`http://127.0.0.1:${port}/${query}${fragment}`);
        });
    }

    _launch(uri) {
        try {
            Gio.AppInfo.launch_default_for_uri(uri, global.create_app_launch_context(0, -1));
        } catch (e) {
            Main.notifyError('Session Pulse', `Couldn't open the browser: ${e.message}`);
        }
    }

    destroy() {
        this._cancellable.cancel();
        if (this._pollId) {
            GLib.Source.remove(this._pollId);
            this._pollId = 0;
        }
        this._monitor?.cancel();
        this._monitor = null;
        super.destroy();
    }
});

export default class SessionPulseExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
