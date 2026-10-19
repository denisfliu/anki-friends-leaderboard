// Friends Leaderboard server (API version 1).
//
// Paste this into the sheet's Apps Script editor once (Extensions > Apps Script),
// run setup(), and deploy it as a web app. It's written so it never needs editing:
//
// - New stats need no server change. Any new field the add-on sends becomes a
//   new column automatically.
// - Tabs and columns are created when missing and found by header name, so
//   sorting rows, moving columns or deleting a tab doesn't break anything.
// - Running the group (invite code, renames, lost tokens, removing people,
//   announcements) happens through admin actions from the add-on.
// - A small post feed and group settings leave room for challenges, update
//   notices and similar features without new server code.
//
// Please don't put formulas in the Players, Daily or Posts tabs; the script
// rewrites those cells as plain values. Build charts or summaries on your own
// tabs that reference these ones.

const API_VERSION = 1;
const SERVER_VERSION = '1.0.0';

const TABS = {
  players: { name: 'Players', core: ['id', 'name', 'admin', 'active', 'joined', 'updated', 'token_hash'] },
  daily: { name: 'Daily', core: ['date', 'player', 'name', 'updated'] },
  posts: { name: 'Posts', core: ['id', 'time', 'player', 'name', 'kind', 'data'] },
};

const LIMITS = {
  bodyChars: 500000,
  players: 100,
  nameChars: 24,
  extraColumns: 80, // stat columns per tab beyond the core ones
  stringChars: 1000,
  uploadDays: 400,
  postChars: 10000,
  feedRows: 500,
  settingsChars: 8000,
  joinsPerHour: 20,
  uploadGapSeconds: 5,
  postsPerMinute: 10,
};

const FIELD_KEY = /^[a-z][A-Za-z0-9_]{0,39}$/;
const KIND = /^[a-z][a-z0-9_-]{0,31}$/;
const DATE = /^(19|20|21)\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;
const DEFAULT_BOARD_DAYS = 16;

// ---------------------------------------------------------------------------
// Entry points

// Run once from the editor to authorize the script and create the tabs.
// Safe to run again; it never deletes data.
function setup() {
  Object.keys(TABS).forEach(table_);
  const ss = SpreadsheetApp.getActive();
  const blank = ss.getSheetByName('Sheet1');
  if (blank && blank.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(blank);
  Logger.log('Invite code: ' + settings_().inviteCode);
  Logger.log('Next: Deploy > New deployment > Web app, execute as Me, access Anyone.');
}

function doGet() {
  return json_(ok_({ service: 'friends-leaderboard' }));
}

function doPost(e) {
  return json_(handle_(e && e.postData ? e.postData.contents : ''));
}

function handle_(raw) {
  if (!raw) return err_('bad_json');
  if (raw.length > LIMITS.bodyChars) return err_('too_large');
  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    return err_('bad_json');
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return err_('bad_json');

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return err_('busy');
  try {
    const action = String(body.action || '');
    if (action === 'ping') return ok_({});
    if (action === 'join') return join_(body);
    const actions = {
      info: info_, board: board_, upload: upload_, rename: rename_, leave: leave_,
      post: post_, feed: feed_, deletePost: deletePost_, admin: admin_,
    };
    if (!actions[action]) return err_('unknown_action');
    const me = auth_(body.token);
    if (!me) return err_('bad_token');
    return actions[action](body, me);
  } catch (e) {
    return err_('server_error', String((e && e.message) || e));
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Players

function join_(body) {
  const settings = settings_();
  if (!settings.inviteCode || String(body.invite || '').trim() !== settings.inviteCode) {
    return err_('bad_invite');
  }
  if (!countWithin_('joins', 3600, LIMITS.joinsPerHour)) return err_('slow_down');
  const name = cleanName_(body.name);
  if (!name) return err_('bad_name');

  const t = table_('players');
  const rows = readRows_(t);
  const token = Utilities.getUuid() + Utilities.getUuid();
  const existing = rows.findIndex(r => sameName_(get_(t, r, 'name'), name));

  if (existing >= 0) {
    // A name whose token was cleared (left, or reset by an admin) can be
    // claimed again, which keeps that person's history.
    const row = rows[existing];
    if (get_(t, row, 'token_hash')) return err_('name_taken');
    set_(t, row, { token_hash: hash_(token), active: true, updated: new Date() });
    writeRows_(t, rows, existing, existing);
    touch_();
    return ok_({ token: token, player: publicPlayer_(t, row) });
  }

  if (rows.filter(r => isActive_(get_(t, r, 'active'))).length >= LIMITS.players) {
    return err_('group_full');
  }
  const isFirstAdmin = !rows.some(r => isTrue_(get_(t, r, 'admin')));
  const row = blankRow_(t);
  set_(t, row, {
    id: 'p' + Utilities.getUuid().replace(/-/g, '').slice(0, 10),
    name: name,
    admin: isFirstAdmin,
    active: true,
    joined: new Date(),
    updated: new Date(),
    token_hash: hash_(token),
  });
  appendRows_(t, [row]);
  touch_();
  return ok_({ token: token, player: publicPlayer_(t, row) });
}

function info_(body, me) {
  return ok_({ me: publicPlayer_(me.t, me.row), settings: settingsFor_(me) });
}

function rename_(body, me) {
  const name = cleanName_(body.name);
  if (!name) return err_('bad_name');
  const err = renamePlayer_(me, me.i, name);
  if (err) return err_(err);
  return ok_({ me: publicPlayer_(me.t, me.row) });
}

// Hides the player and frees their name. Joining again with the same name
// brings their history back.
function leave_(body, me) {
  set_(me.t, me.row, { token_hash: '', active: false, updated: new Date() });
  writeRows_(me.t, me.rows, me.i, me.i);
  touch_();
  return ok_({});
}

function auth_(token) {
  if (!token || typeof token !== 'string') return null;
  const t = table_('players');
  const rows = readRows_(t);
  const h = hash_(token);
  const i = rows.findIndex(r => get_(t, r, 'token_hash') === h);
  if (i < 0 || !isActive_(get_(t, rows[i], 'active'))) return null;
  const row = rows[i];
  return {
    t: t, rows: rows, i: i, row: row,
    id: String(get_(t, row, 'id')),
    name: String(get_(t, row, 'name')),
    admin: isTrue_(get_(t, row, 'admin')),
  };
}

function renamePlayer_(me, i, name) {
  const t = me.t;
  const rows = me.rows;
  if (rows.some((r, j) => j !== i && sameName_(get_(t, r, 'name'), name))) return 'name_taken';
  const id = String(get_(t, rows[i], 'id'));
  set_(t, rows[i], { name: name, updated: new Date() });
  writeRows_(t, rows, i, i);
  // Keep the readable name column in the other tabs in step.
  ['daily', 'posts'].forEach(key => {
    const other = table_(key);
    const otherRows = readRows_(other);
    const hits = [];
    otherRows.forEach((r, j) => {
      if (String(get_(other, r, 'player')) === id) {
        set_(other, r, { name: name });
        hits.push(j);
      }
    });
    if (hits.length) writeColumn_(other, otherRows, 'name', hits[0], hits[hits.length - 1]);
  });
  touch_();
  return null;
}

// ---------------------------------------------------------------------------
// Stats

// totals/profile merge into the player's row; days merge into one Daily row
// per (date, player). Fields that aren't sent keep their old values.
function upload_(body, me) {
  if (!gapSince_('upload:' + me.id, LIMITS.uploadGapSeconds)) return err_('slow_down');
  const ignored = [];

  const fields = cleanFields_(Object.assign({}, body.profile, body.totals), TABS.players.core);
  addColumns_(me.t, Object.keys(fields), ignored);
  set_(me.t, me.row, Object.assign(fields, { updated: new Date() }));
  writeRows_(me.t, me.rows, me.i, me.i);

  const days = (Array.isArray(body.days) ? body.days : [])
    .filter(d => d && typeof d === 'object' && DATE.test(String(d.date)))
    .slice(-LIMITS.uploadDays)
    .map(d => ({ date: d.date, fields: cleanFields_(d, TABS.daily.core) }));

  if (days.length) {
    const t = table_('daily');
    const keys = {};
    days.forEach(d => Object.keys(d.fields).forEach(k => { keys[k] = true; }));
    addColumns_(t, Object.keys(keys), ignored);

    const rows = readRows_(t);
    const index = {};
    rows.forEach((r, i) => {
      if (String(get_(t, r, 'player')) === me.id) index[dateStr_(get_(t, r, 'date'))] = i;
    });

    const now = new Date();
    const fresh = [];
    const freshByDate = {};
    let lo = Infinity;
    let hi = -1;
    days.forEach(d => {
      const values = Object.assign({}, d.fields, { date: d.date, player: me.id, name: me.name, updated: now });
      const i = index[d.date];
      if (i !== undefined) {
        set_(t, rows[i], values);
        lo = Math.min(lo, i);
        hi = Math.max(hi, i);
      } else if (freshByDate[d.date]) {
        set_(t, freshByDate[d.date], values);
      } else {
        const row = blankRow_(t);
        set_(t, row, values);
        fresh.push(row);
        freshByDate[d.date] = row;
      }
    });
    if (hi >= 0) writeRows_(t, rows, lo, hi);
    appendRows_(t, fresh);
  }

  touch_();
  return ok_({ ignored: ignored });
}

// Returns players plus Daily rows from `since` (default about two weeks ago)
// to `until`. Send back `lastWrite` as `ifChangedSince` to get a tiny
// "unchanged" answer when nothing new has been uploaded.
function board_(body, me) {
  const lastWrite = PropertiesService.getScriptProperties().getProperty('LAST_WRITE') || '';
  if (body.ifChangedSince && body.ifChangedSince === lastWrite) {
    return ok_({ unchanged: true, lastWrite: lastWrite });
  }

  const includeInactive = me.admin && body.includeInactive === true;
  const players = me.rows
    .filter(r => get_(me.t, r, 'id') !== '' && (includeInactive || isActive_(get_(me.t, r, 'active'))))
    .map(r => publicPlayer_(me.t, r));
  const ids = {};
  players.forEach(p => { ids[p.id] = true; });

  const since = DATE.test(String(body.since)) ? body.since : daysAgo_(DEFAULT_BOARD_DAYS);
  const until = DATE.test(String(body.until)) ? body.until : '9999-12-31';
  const t = table_('daily');
  const days = [];
  readRows_(t).forEach(r => {
    const date = dateStr_(get_(t, r, 'date'));
    if (date >= since && date <= until && ids[String(get_(t, r, 'player'))]) {
      const obj = rowObject_(t, r, ['name', 'updated']);
      obj.date = date;
      days.push(obj);
    }
  });

  return ok_({
    me: publicPlayer_(me.t, me.row),
    players: players,
    days: days,
    settings: settingsFor_(me),
    lastWrite: lastWrite,
  });
}

// ---------------------------------------------------------------------------
// Posts: a small shared feed for future features (challenges, shout-outs,
// weekly results). `kind` says what a post is; `data` is any JSON.

function post_(body, me) {
  const kind = String(body.kind || '');
  if (!KIND.test(kind)) return err_('bad_kind');
  const data = JSON.stringify(body.data === undefined ? null : body.data);
  if (data.length > LIMITS.postChars) return err_('too_large');
  if (!countWithin_('posts:' + me.id, 60, LIMITS.postsPerMinute)) return err_('slow_down');

  const t = table_('posts');
  const row = blankRow_(t);
  set_(t, row, {
    id: 'm' + Utilities.getUuid().replace(/-/g, '').slice(0, 12),
    time: new Date(),
    player: me.id,
    name: me.name,
    kind: kind,
    data: data,
  });
  appendRows_(t, [row]);
  touch_();
  return ok_({ post: postObject_(t, row) });
}

function feed_(body, me) {
  const t = table_('posts');
  const since = body.since ? new Date(body.since) : null;
  const kinds = Array.isArray(body.kinds) ? body.kinds.map(String) : null;
  const limit = Math.max(1, Math.min(LIMITS.feedRows, Number(body.limit) || 100));
  const posts = readRows_(t)
    .filter(r => get_(t, r, 'id') !== '')
    .filter(r => !kinds || kinds.indexOf(String(get_(t, r, 'kind'))) >= 0)
    .filter(r => !since || isNaN(since) || toDate_(get_(t, r, 'time')) > since)
    .map(r => postObject_(t, r))
    .sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return ok_({ posts: posts.slice(-limit) });
}

function deletePost_(body, me) {
  const t = table_('posts');
  const rows = readRows_(t);
  const i = rows.findIndex(r => String(get_(t, r, 'id')) === String(body.id));
  if (i < 0) return err_('not_found');
  if (String(get_(t, rows[i], 'player')) !== me.id && !me.admin) return err_('forbidden');
  t.sheet.deleteRow(i + 2);
  touch_();
  return ok_({});
}

// ---------------------------------------------------------------------------
// Admin. The first person to join is an admin; admins can promote others.
//
//   { op: 'settings', set: {key: value}, unset: [key] }
//       Group settings everyone receives with the board, such as
//       announcement, latestVersion or seasonStart. inviteCode is only shown
//       to admins; setting it changes the invite code.
//   { op: 'player', id, name?, admin?, active?, resetToken? }
//       resetToken lets that person join again under the same name and keep
//       their history. active: false hides them and signs them out.
//   { op: 'purge', id }
//       Deletes a player and all of their rows.

function admin_(body, me) {
  if (!me.admin) return err_('forbidden');
  const op = String(body.op || '');

  if (op === 'settings') {
    const s = settings_();
    const set = body.set && typeof body.set === 'object' ? body.set : {};
    Object.keys(set).forEach(k => {
      if (FIELD_KEY.test(k)) s[k] = k === 'inviteCode' ? String(set[k]).trim() : set[k];
    });
    (Array.isArray(body.unset) ? body.unset : []).forEach(k => {
      if (k !== 'inviteCode') delete s[k];
    });
    if (!s.inviteCode) return err_('bad_invite');
    const saved = saveSettings_(s);
    if (saved) return err_(saved);
    touch_();
    return ok_({ settings: settingsFor_(me) });
  }

  const i = me.rows.findIndex(r => String(get_(me.t, r, 'id')) === String(body.id));
  if (i < 0) return err_('not_found');
  const row = me.rows[i];
  const id = String(get_(me.t, row, 'id'));

  if (op === 'player') {
    if (body.name !== undefined) {
      const name = cleanName_(body.name);
      if (!name) return err_('bad_name');
      const err = renamePlayer_(me, i, name);
      if (err) return err_(err);
    }
    const changes = { updated: new Date() };
    if (body.admin !== undefined) {
      if (!body.admin && id === me.id && !me.rows.some((r, j) => j !== i && isTrue_(get_(me.t, r, 'admin')) && isActive_(get_(me.t, r, 'active')))) {
        return err_('last_admin');
      }
      changes.admin = body.admin === true;
    }
    if (body.active !== undefined) {
      if (body.active !== true && id === me.id) return err_('forbidden'); // use leave instead
      changes.active = body.active === true;
      if (!changes.active) changes.token_hash = '';
    }
    if (body.resetToken === true) changes.token_hash = '';
    set_(me.t, row, changes);
    writeRows_(me.t, me.rows, i, i);
    touch_();
    return ok_({ player: publicPlayer_(me.t, row) });
  }

  if (op === 'purge') {
    if (id === me.id) return err_('forbidden');
    ['daily', 'posts'].forEach(key => {
      const t = table_(key);
      deleteWhere_(t, readRows_(t), r => String(get_(t, r, 'player')) === id);
    });
    deleteWhere_(me.t, me.rows, r => String(get_(me.t, r, 'id')) === id);
    touch_();
    return ok_({});
  }

  return err_('unknown_op');
}

// ---------------------------------------------------------------------------
// Settings

function settings_() {
  const props = PropertiesService.getScriptProperties();
  let s = {};
  try {
    s = JSON.parse(props.getProperty('SETTINGS') || '{}') || {};
  } catch (e) {
    s = {};
  }
  if (!s.inviteCode) {
    s.inviteCode = Utilities.getUuid().slice(0, 8);
    props.setProperty('SETTINGS', JSON.stringify(s));
  }
  return s;
}

function saveSettings_(s) {
  const text = JSON.stringify(s);
  if (text.length > LIMITS.settingsChars) return 'too_large';
  PropertiesService.getScriptProperties().setProperty('SETTINGS', text);
  return null;
}

function settingsFor_(me) {
  const s = settings_();
  if (!me.admin) delete s.inviteCode;
  return s;
}

// ---------------------------------------------------------------------------
// Sheet access. Columns are looked up by header name on every request.

function table_(key) {
  const def = TABS[key];
  const ss = SpreadsheetApp.getActive();
  const sheet = ss.getSheetByName(def.name) || ss.insertSheet(def.name);
  const width = sheet.getLastColumn();
  const headers = width ? sheet.getRange(1, 1, 1, width).getValues()[0].map(h => String(h).trim()) : [];
  const t = { key: key, sheet: sheet, headers: headers, col: {} };
  headers.forEach((h, i) => {
    if (h && !(h in t.col)) t.col[h] = i;
  });
  addColumns_(t, def.core, null);
  if (sheet.getFrozenRows() !== 1) sheet.setFrozenRows(1);
  return t;
}

function addColumns_(t, keys, ignored) {
  const core = TABS[t.key].core;
  const extras = Object.keys(t.col).filter(h => core.indexOf(h) < 0).length;
  const missing = keys.filter(k => !(k in t.col));
  const room = ignored ? Math.max(0, LIMITS.extraColumns - extras) : missing.length;
  const adding = missing.slice(0, room);
  if (ignored) missing.slice(room).forEach(k => ignored.push(k));
  if (!adding.length) return;

  const start = t.headers.length;
  const need = start + adding.length;
  if (need > t.sheet.getMaxColumns()) {
    t.sheet.insertColumnsAfter(t.sheet.getMaxColumns(), need - t.sheet.getMaxColumns());
  }
  t.sheet.getRange(1, start + 1, 1, adding.length).setValues([adding]).setFontWeight('bold');
  adding.forEach((k, j) => {
    t.headers.push(k);
    t.col[k] = start + j;
  });
}

function readRows_(t) {
  const last = t.sheet.getLastRow();
  if (last < 2 || !t.headers.length) return [];
  return t.sheet.getRange(2, 1, last - 1, t.headers.length).getValues();
}

function blankRow_(t) {
  return t.headers.map(() => '');
}

function get_(t, row, key) {
  const i = t.col[key];
  return i === undefined || i >= row.length ? '' : row[i];
}

function set_(t, row, values) {
  Object.keys(values).forEach(k => {
    const i = t.col[k];
    if (i === undefined) return;
    while (row.length <= i) row.push('');
    row[i] = values[k];
  });
}

// Writes rows[lo..hi] (0-based data rows) back to the sheet.
function writeRows_(t, rows, lo, hi) {
  const out = rows.slice(lo, hi + 1).map(r => pad_(t, r).map(cellOut_));
  t.sheet.getRange(lo + 2, 1, out.length, t.headers.length).setValues(out);
}

function writeColumn_(t, rows, key, lo, hi) {
  const c = t.col[key];
  const out = rows.slice(lo, hi + 1).map(r => [cellOut_(r[c] === undefined ? '' : r[c])]);
  t.sheet.getRange(lo + 2, c + 1, out.length, 1).setValues(out);
}

function appendRows_(t, rows) {
  if (!rows.length) return;
  const start = t.sheet.getLastRow() + 1;
  const end = start + rows.length - 1;
  if (end > t.sheet.getMaxRows()) {
    t.sheet.insertRowsAfter(t.sheet.getMaxRows(), end - t.sheet.getMaxRows());
  }
  t.sheet.getRange(start, 1, rows.length, t.headers.length)
    .setValues(rows.map(r => pad_(t, r).map(cellOut_)));
}

function deleteWhere_(t, rows, test) {
  // Delete from the bottom up, in runs, so row numbers stay valid.
  let i = rows.length - 1;
  while (i >= 0) {
    if (!test(rows[i])) {
      i--;
      continue;
    }
    let j = i;
    while (j - 1 >= 0 && test(rows[j - 1])) j--;
    t.sheet.deleteRows(j + 2, i - j + 1);
    i = j - 1;
  }
}

function pad_(t, row) {
  const r = row.slice(0, t.headers.length);
  while (r.length < t.headers.length) r.push('');
  return r;
}

// A leading apostrophe stores text as-is, so Sheets never turns names into
// formulas or "2026-10-19" into a date value.
function cellOut_(v) {
  return typeof v === 'string' && v !== '' ? "'" + v : v;
}

function rowObject_(t, row, omit) {
  const obj = {};
  t.headers.forEach((h, i) => {
    if (!h || (omit && omit.indexOf(h) >= 0)) return;
    const v = row[i];
    obj[h] = v instanceof Date ? v.toISOString() : v;
  });
  return obj;
}

function publicPlayer_(t, row) {
  const obj = rowObject_(t, row, ['token_hash']);
  obj.id = String(obj.id);
  obj.name = String(obj.name);
  obj.admin = isTrue_(obj.admin);
  obj.active = isActive_(obj.active);
  return obj;
}

function postObject_(t, row) {
  const obj = rowObject_(t, row, null);
  try {
    obj.data = JSON.parse(String(obj.data));
  } catch (e) {
    obj.data = null;
  }
  obj.time = toDate_(get_(t, row, 'time')).toISOString();
  return obj;
}

// ---------------------------------------------------------------------------
// Small helpers

function cleanName_(v) {
  const name = String(v || '').replace(/\s+/g, ' ').trim();
  if (!name || name.length > LIMITS.nameChars || /[\u0000-\u001f]/.test(name)) return '';
  return name;
}

function sameName_(a, b) {
  return String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
}

function cleanFields_(obj, reserved) {
  const out = {};
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return out;
  Object.keys(obj).forEach(k => {
    if (!FIELD_KEY.test(k) || reserved.indexOf(k) >= 0) return;
    const v = cleanValue_(obj[k]);
    if (v !== undefined) out[k] = v;
  });
  return out;
}

function cleanValue_(v) {
  if (v === null) return '';
  if (typeof v === 'number') return Number.isFinite(v) ? Math.max(-1e15, Math.min(1e15, v)) : undefined;
  if (typeof v === 'boolean') return v;
  if (typeof v === 'string') return v.slice(0, LIMITS.stringChars);
  if (typeof v === 'object') {
    const s = JSON.stringify(v);
    return s.length <= LIMITS.stringChars ? s : undefined;
  }
  return undefined;
}

function isTrue_(v) {
  return v === true || String(v).toUpperCase() === 'TRUE';
}

function isActive_(v) {
  return v === '' || isTrue_(v);
}

function dateStr_(v) {
  if (v instanceof Date) {
    return Utilities.formatDate(v, SpreadsheetApp.getActive().getSpreadsheetTimeZone(), 'yyyy-MM-dd');
  }
  return String(v || '');
}

function toDate_(v) {
  const d = v instanceof Date ? v : new Date(v);
  return isNaN(d) ? new Date(0) : d;
}

function daysAgo_(n) {
  return Utilities.formatDate(
    new Date(Date.now() - n * 86400000),
    SpreadsheetApp.getActive().getSpreadsheetTimeZone(),
    'yyyy-MM-dd'
  );
}

// Only hashes are stored, so sharing the sheet never leaks anyone's token.
function hash_(token) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, token, Utilities.Charset.UTF_8)
    .map(b => ((b + 256) % 256).toString(16).padStart(2, '0'))
    .join('');
}

function gapSince_(key, seconds) {
  const cache = CacheService.getScriptCache();
  if (cache.get(key)) return false;
  cache.put(key, '1', seconds);
  return true;
}

function countWithin_(key, seconds, max) {
  const cache = CacheService.getScriptCache();
  const n = Number(cache.get(key)) || 0;
  if (n >= max) return false;
  cache.put(key, String(n + 1), seconds);
  return true;
}

function touch_() {
  PropertiesService.getScriptProperties().setProperty('LAST_WRITE', new Date().toISOString());
}

function ok_(obj) {
  return Object.assign({ ok: true, api: API_VERSION, server: SERVER_VERSION }, obj);
}

function err_(code, detail) {
  const out = { ok: false, api: API_VERSION, server: SERVER_VERSION, error: code };
  if (detail) out.detail = detail;
  return out;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
