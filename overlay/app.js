/* KiCad Live dashboard.
 *
 * Connects to the sync server as a read-only "dashboard" client: it sends a
 * hello with dashboard:true, so the server never counts it as a designer and
 * never expects board changes from it.
 */
(function () {
  "use strict";

  var PALETTE = ["#35c98a", "#4da3ff", "#e8a33d", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c"];
  var MAX_FEED = 60;

  var state = { project: null, clients: [], locks: [], version: 0, objects: 0 };
  var feed = [];
  var ws = null;
  var retry = 0;

  var el = function (id) { return document.getElementById(id); };

  function colourFor(key) {
    var hash = 0;
    for (var i = 0; i < key.length; i++) { hash = (hash * 31 + key.charCodeAt(i)) | 0; }
    return PALETTE[Math.abs(hash) % PALETTE.length];
  }

  function initials(name) {
    var parts = String(name || "?").trim().split(/\s+/);
    if (parts.length === 1) { return parts[0].slice(0, 2).toUpperCase(); }
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function clockOf(ts) {
    var d = ts ? new Date(ts * 1000) : new Date();
    return d.toTimeString().slice(0, 8);
  }

  /* ------------------------------------------------------------- project id */

  // The dashboard follows one project. Default to the demo, allow ?project=name.
  function projectId() {
    var match = /[?&]project=([A-Za-z0-9_.-]{1,64})/.exec(window.location.search);
    return match ? match[1] : "demo_board";
  }

  /* ------------------------------------------------------------- connection */

  function connect() {
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    setConn(false, "Connecting…");
    ws = new WebSocket(url);

    ws.onopen = function () {
      retry = 0;
      setConn(true, "Connected");
      ws.send(JSON.stringify({
        type: "hello",
        client_id: "dashboard-" + Math.random().toString(36).slice(2, 10),
        user_name: "Dashboard",
        project_id: projectId(),
        dashboard: true
      }));
    };

    ws.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      handle(message);
    };

    ws.onclose = function () {
      setConn(false, "Disconnected — retrying…");
      var delay = Math.min(1000 * Math.pow(1.6, retry++), 10000);
      setTimeout(connect, delay);
    };

    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function setConn(ok, text) {
    el("conn-dot").className = "dot " + (ok ? "on" : "off");
    el("conn-text").textContent = text;
  }

  /* ---------------------------------------------------------------- handlers */

  function handle(message) {
    switch (message.type) {
      case "welcome":
        state.project = message.project_id;
        pushFeed({ kind: "system", who: "", what: "dashboard connected to " + message.project_id, ts: message.ts });
        break;

      case "project_state":
        state.project = message.project_id;
        state.version = message.version || 0;
        state.objects = Object.keys(message.objects || {}).length;
        state.locks = message.locks || [];
        state.clients = message.clients || [];
        break;

      case "presence_update":
        state.clients = message.clients || [];
        break;

      case "lock_update":
        state.locks = message.locks || [];
        break;

      case "history":
        (message.entries || []).slice().reverse().forEach(function (entry) {
          pushFeed({
            kind: "change", who: entry.user_name, what: entry.summary,
            ts: entry.ts, version: entry.version, key: "v" + entry.version
          });
          if (entry.version > state.version) { state.version = entry.version; }
        });
        break;

      case "remote_change":
        (message.changes || []).forEach(function (change) {
          if (change.version > state.version) { state.version = change.version; }
        });
        break;

      case "conflict_event":
        (message.conflicts || []).forEach(function (conflict) {
          pushFeed({
            kind: "conflict", who: message.user_name,
            what: "CONFLICT on " + conflict.reference + "." + conflict.field +
                  " — also changed by " + conflict.conflicting_user,
            ts: message.ts
          });
        });
        break;

      case "blocked_event":
        (message.blocked || []).forEach(function (blocked) {
          pushFeed({
            kind: "blocked", who: message.user_name,
            what: "blocked from editing " + blocked.reference +
                  " — locked by " + blocked.owner_name,
            ts: message.ts
          });
        });
        break;

      default:
        return;
    }
    render();
  }

  function pushFeed(entry) {
    // History is replayed on reconnect; do not duplicate entries we already show.
    if (entry.key && feed.some(function (e) { return e.key === entry.key; })) { return; }
    entry.isNew = true;
    feed.unshift(entry);
    if (feed.length > MAX_FEED) { feed.length = MAX_FEED; }
  }

  /* ------------------------------------------------------------------ render */

  function render() {
    el("stat-project").textContent = state.project || "—";
    el("stat-version").textContent = state.version;
    el("stat-objects").textContent = state.objects;

    var online = state.clients.filter(function (c) { return c.online; });
    el("stat-clients").textContent = online.length;
    el("stat-locks").textContent = state.locks.length;

    renderUsers();
    renderLocks();
    renderFeed();
  }

  function renderUsers() {
    var host = el("users");
    host.innerHTML = "";
    if (!state.clients.length) {
      host.innerHTML = '<li class="empty">Nobody connected yet.</li>';
      return;
    }
    state.clients.forEach(function (client) {
      var li = document.createElement("li");
      if (!client.online) { li.className = "offline"; }

      var avatar = document.createElement("span");
      avatar.className = "avatar";
      avatar.style.background = client.online ? colourFor(client.client_id) : "#3a4453";
      avatar.textContent = initials(client.user_name);

      var name = document.createElement("span");
      name.className = "name";
      name.textContent = client.user_name;

      var status = document.createElement("span");
      status.className = "status";
      status.textContent = client.online ? client.status_text : "Offline";

      li.appendChild(avatar);
      li.appendChild(name);
      li.appendChild(status);
      host.appendChild(li);
    });
  }

  function renderLocks() {
    var host = el("locks");
    host.innerHTML = "";
    if (!state.locks.length) {
      host.innerHTML = '<li class="empty">No components locked.</li>';
      return;
    }
    state.locks.forEach(function (lock) {
      var li = document.createElement("li");

      var ref = document.createElement("span");
      ref.className = "chip ref";
      ref.textContent = lock.reference || lock.uuid.slice(0, 8);

      var arrow = document.createElement("span");
      arrow.textContent = "→";
      arrow.style.color = "#8b97a8";

      var owner = document.createElement("span");
      owner.className = "name";
      owner.textContent = lock.owner_name;

      var expiry = document.createElement("span");
      expiry.className = "status";
      expiry.textContent = Math.round(lock.expires_in) + "s left";

      li.appendChild(ref);
      li.appendChild(arrow);
      li.appendChild(owner);
      li.appendChild(expiry);
      host.appendChild(li);
    });
  }

  function renderFeed() {
    var host = el("feed");
    host.innerHTML = "";
    if (!feed.length) {
      host.innerHTML = '<li class="empty">Waiting for changes…</li>';
      return;
    }
    feed.forEach(function (entry) {
      var li = document.createElement("li");
      li.className = entry.kind + (entry.isNew ? " new" : "");
      entry.isNew = false;

      var time = document.createElement("span");
      time.className = "time";
      time.textContent = clockOf(entry.ts);

      var who = document.createElement("span");
      who.className = "who";
      who.textContent = entry.who || "";
      if (entry.who) { who.style.color = colourFor(entry.who); }

      var what = document.createElement("span");
      what.className = "what";
      what.textContent = entry.what;

      li.appendChild(time);
      if (entry.who) { li.appendChild(who); }
      li.appendChild(what);

      if (entry.version) {
        var version = document.createElement("span");
        version.className = "ver";
        version.textContent = "v" + entry.version;
        li.appendChild(version);
      }
      host.appendChild(li);
    });
  }

  // Keep lock countdowns ticking without waiting for a server message.
  setInterval(function () {
    var changed = false;
    state.locks.forEach(function (lock) {
      if (lock.expires_in > 0) { lock.expires_in -= 1; changed = true; }
    });
    if (changed) { renderLocks(); }
  }, 1000);

  render();
  connect();
})();
