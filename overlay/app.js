/* KiCad Live dashboard.
 *
 * One script for every page; `document.body.dataset.page` selects the view.
 * No framework and no build step - the demo machines should need nothing
 * installed beyond a browser.
 *
 * The WebSocket carries live deltas; REST serves page loads. Nothing here
 * contacts any external service.
 */
(function () {
  "use strict";

  var PALETTE = ["#35c98a", "#4da3ff", "#e8a33d", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c"];

  var State = {
    project: projectFromUrl(),
    me: null,                 // dashboard's own client id
    clients: [], locks: [], events: [], threads: [],
    counts: { open: 0, resolved: 0, total: 0 },
    overview: null, summary: null, whatsNew: null,
    connected: false
  };

  var ws = null, retry = 0;

  /* ------------------------------------------------------------- helpers */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }

  function projectFromUrl() {
    var m = /[?&]project=([A-Za-z0-9_.-]{1,64})/.exec(location.search);
    return m ? m[1] : "demo_board";
  }

  function colourFor(key) {
    var h = 0, s = String(key || "?");
    for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
    return PALETTE[Math.abs(h) % PALETTE.length];
  }

  function initials(name) {
    var p = String(name || "?").trim().split(/\s+/);
    return (p.length === 1 ? p[0].slice(0, 2) : p[0][0] + p[p.length - 1][0]).toUpperCase();
  }

  function clockOf(ts) {
    return (ts ? new Date(ts * 1000) : new Date()).toTimeString().slice(0, 8);
  }

  function agoOf(ts) {
    if (!ts) { return ""; }
    var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) { return s + "s ago"; }
    if (s < 3600) { return Math.floor(s / 60) + "m ago"; }
    if (s < 86400) { return Math.floor(s / 3600) + "h ago"; }
    return Math.floor(s / 86400) + "d ago";
  }

  function avatar(name, key, small) {
    var a = el("span", "avatar" + (small ? " sm" : ""), initials(name));
    a.style.background = colourFor(key || name);
    return a;
  }

  function domainChip(domain) {
    var d = domain || "project";
    var cls = d === "schematic" ? "chip sch" : d === "pcb" ? "chip pcb" : "chip";
    return el("span", cls, d === "schematic" ? "SCH" : d === "pcb" ? "PCB" : d.toUpperCase());
  }

  function api(path, params) {
    var q = new URLSearchParams(Object.assign({ project: State.project }, params || {}));
    return fetch("/api/" + path + "?" + q.toString()).then(function (r) {
      if (!r.ok) { throw new Error(path + " -> HTTP " + r.status); }
      return r.json();
    });
  }

  function send(payload) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(payload)); return true; }
    return false;
  }

  /* --------------------------------------------------------------- toasts */

  function toast(text, kind) {
    var host = $(".toasts") || document.body.appendChild(el("div", "toasts"));
    var t = el("div", "toast" + (kind ? " " + kind : ""), text);
    host.appendChild(t);
    setTimeout(function () {
      t.style.transition = "opacity .3s"; t.style.opacity = "0";
      setTimeout(function () { t.remove(); }, 320);
    }, 6000);
  }

  /* ---------------------------------------------------------------- shell */

  var NAV = [
    { href: "index.html", icon: "◈", label: "Overview" },
    { href: "schematic.html", icon: "⌘", label: "Schematic" },
    { href: "pcb.html", icon: "▦", label: "PCB" },
    { href: "activity.html", icon: "≡", label: "Activity" },
    { href: "comments.html", icon: "◍", label: "Comments", badge: "comments" },
    { href: "project.html", icon: "⚙", label: "Hardware Summary" }
  ];

  function renderShell() {
    var nav = $("nav");
    if (!nav) { return; }
    clear(nav);
    nav.appendChild(el("div", "section", "Project"));
    var page = document.body.dataset.page;
    NAV.forEach(function (item) {
      var a = el("a");
      a.href = item.href + "?project=" + encodeURIComponent(State.project);
      if (item.href.indexOf(page) === 0) { a.className = "active"; }
      a.appendChild(el("span", "ico", item.icon));
      a.appendChild(el("span", null, item.label));
      if (item.badge === "comments" && State.counts.open > 0) {
        a.appendChild(el("span", "badge", String(State.counts.open)));
      }
      nav.appendChild(a);
    });
  }

  function renderTop() {
    var name = $("#project-name");
    if (name) { name.textContent = State.project; }
    var dot = $("#conn-dot"), txt = $("#conn-text");
    if (dot) { dot.className = "dot " + (State.connected ? "on" : "off"); }
    if (txt) { txt.textContent = State.connected ? "Connected" : "Disconnected"; }
    var users = $("#online-count");
    if (users) {
      users.textContent = State.clients.filter(function (c) { return c.online; }).length + " online";
    }
  }

  /* ------------------------------------------------------------ websocket */

  function connect() {
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    ws = new WebSocket(url);

    ws.onopen = function () {
      retry = 0;
      State.connected = true;
      State.me = "dashboard-" + Math.random().toString(36).slice(2, 10);
      send({ type: "hello", client_id: State.me, user_name: "Dashboard",
             project_id: State.project, dashboard: true, role: "manager" });
      renderTop();
    };

    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handle(msg);
    };

    ws.onclose = function () {
      State.connected = false;
      renderTop();
      setTimeout(connect, Math.min(1000 * Math.pow(1.6, retry++), 10000));
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function handle(msg) {
    switch (msg.type) {
      case "presence_update":
        State.clients = msg.clients || [];
        renderTop(); repaint("users");
        break;
      case "lock_update":
        State.locks = msg.locks || [];
        repaint("locks");
        break;
      case "activity":
        mergeEvents(msg.events || []);
        repaint("activity");
        break;
      case "comments":
        State.threads = msg.threads || [];
        State.counts = msg.counts || State.counts;
        renderShell(); repaint("comments");
        break;
      case "comment_created":
        State.counts = msg.counts || State.counts;
        toast((msg.comment.author_name) + " commented on " +
              (msg.comment.object_ref || "an object"));
        refreshComments(); renderShell();
        break;
      case "comment_updated":
        State.counts = msg.counts || State.counts;
        toast("Comment on " + (msg.comment.object_ref || "an object") + " " + msg.comment.status);
        refreshComments(); renderShell();
        break;
      case "conflict_event":
        (msg.conflicts || []).forEach(function (c) {
          toast("CONFLICT on " + c.reference + "." + c.field +
                " between " + msg.user_name + " and " + c.conflicting_user, "conflict");
        });
        refreshActivity();
        break;
      case "blocked_event":
        (msg.blocked || []).forEach(function (b) {
          toast(msg.user_name + " was blocked from " + b.reference +
                " (locked by " + b.owner_name + ")", "lock");
        });
        refreshActivity();
        break;
      case "remote_change":
        refreshActivity();
        break;
      case "project_state":
        if (msg.version !== undefined) { setStat("stat-version", msg.version); }
        break;
      default: break;
    }
  }

  function mergeEvents(incoming) {
    var seen = {};
    State.events.forEach(function (e) { seen[e.event_id] = true; });
    var fresh = incoming.filter(function (e) { return !seen[e.event_id]; });
    State.events = fresh.concat(State.events).slice(0, 400);
  }

  /* --------------------------------------------------------------- paints */

  var painters = {};
  function repaint(key) { if (painters[key]) { painters[key](); } }
  function setStat(id, value) { var n = document.getElementById(id); if (n) { n.textContent = value; } }

  function refreshActivity() {
    api("activity", { limit: 120 }).then(function (d) {
      State.events = d.events || [];
      repaint("activity");
    }).catch(function () {});
  }

  function refreshComments() {
    api("comments", { status: "all", viewer: State.me }).then(function (d) {
      State.threads = d.threads || [];
      State.counts = d.counts || State.counts;
      renderShell(); repaint("comments");
    }).catch(function () {});
  }

  /* ------------------------------------------------------------ feed item */

  function eventRow(e, onClick) {
    var cls = "";
    if (e.action === "conflict_detected") { cls = " conflict"; }
    else if (e.action === "lock_denied") { cls = " blocked"; }
    else if (String(e.action).indexOf("comment") === 0) { cls = " comment"; }

    var li = el("li", "feed-item" + cls);
    li.appendChild(el("span", "time", clockOf(e.ts)));
    var who = el("span", "who", e.username || "system");
    who.style.color = colourFor(e.username || "system");
    li.appendChild(who);
    li.appendChild(el("span", "what", e.description));
    li.appendChild(domainChip(e.domain));
    if (onClick) { li.addEventListener("click", function () { onClick(e); }); }
    else { li.style.cursor = "default"; }
    return li;
  }

  /* ------------------------------------------------------------- overview */

  function pageOverview() {
    painters.activity = function () {
      var host = $("#recent");
      if (!host) { return; }
      clear(host);
      var items = State.events.slice(0, 14);
      if (!items.length) { host.appendChild(el("li", "empty", "No activity yet.")); return; }
      items.forEach(function (e) { host.appendChild(eventRow(e, showEvent)); });
    };

    painters.users = function () {
      var host = $("#users");
      if (!host) { return; }
      clear(host);
      if (!State.clients.length) {
        host.appendChild(el("li", "empty", "Nobody connected yet.")); return;
      }
      State.clients.forEach(function (c) {
        var li = el("li", c.online ? "" : "offline");
        li.appendChild(avatar(c.user_name, c.client_id));
        li.appendChild(el("span", "name", c.user_name));
        li.appendChild(el("span", "meta", c.online ? c.status_text : "Offline"));
        host.appendChild(li);
      });
      setStat("stat-users", State.clients.filter(function (c) { return c.online; }).length);
    };

    painters.locks = function () {
      var host = $("#locks");
      if (!host) { return; }
      clear(host);
      if (!State.locks.length) {
        host.appendChild(el("li", "empty", "No components locked.")); return;
      }
      State.locks.forEach(function (lk) {
        var li = el("li");
        li.appendChild(domainChip(lk.domain));
        li.appendChild(el("span", "chip lock", lk.reference || lk.uuid.slice(0, 8)));
        li.appendChild(el("span", null, "→"));
        li.appendChild(el("span", "name", lk.owner_name));
        li.appendChild(el("span", "meta", Math.round(lk.expires_in) + "s left"));
        host.appendChild(li);
      });
      setStat("stat-locks", State.locks.length);
    };

    api("overview").then(function (d) {
      State.overview = d;
      State.clients = d.clients || [];
      State.locks = d.locks || [];
      mergeEvents(d.recent_activity || []);
      setStat("stat-users", d.clients_online);
      setStat("stat-changes", d.changes_today);
      setStat("stat-locks", d.lock_count);
      setStat("stat-comments", d.open_comments);
      setStat("stat-conflicts", d.open_conflicts);
      setStat("stat-version", d.version);
      renderTop(); repaint("activity"); repaint("users"); repaint("locks");
    }).catch(function (e) { console.error(e); });

    refreshComments();
  }

  /* ------------------------------------------------------------- activity */

  function pageActivity() {
    var filters = { user: "", domain: "", action: "" };

    painters.activity = function () {
      var host = $("#timeline");
      clear(host);
      var rows = State.events.filter(function (e) {
        if (filters.user && e.username !== filters.user) { return false; }
        if (filters.domain && e.domain !== filters.domain) { return false; }
        if (filters.action && e.action !== filters.action) { return false; }
        return true;
      });
      if (!rows.length) { host.appendChild(el("li", "empty", "No matching activity.")); return; }

      var lastDay = null;
      rows.forEach(function (e) {
        var day = new Date(e.ts * 1000).toDateString();
        if (day !== lastDay) {
          lastDay = day;
          var h = el("li", "empty");
          h.style.fontStyle = "normal";
          h.style.fontWeight = "700";
          h.style.color = "var(--muted)";
          h.textContent = day;
          host.appendChild(h);
        }
        host.appendChild(eventRow(e, showEvent));
      });
      $("#count").textContent = rows.length + " event" + (rows.length === 1 ? "" : "s");
    };

    function rebuildUserFilter() {
      var sel = $("#f-user"), current = sel.value;
      var names = {};
      State.events.forEach(function (e) { if (e.username) { names[e.username] = true; } });
      clear(sel);
      sel.appendChild(new Option("All users", ""));
      Object.keys(names).sort().forEach(function (n) { sel.appendChild(new Option(n, n)); });
      sel.value = current;
    }

    ["f-user", "f-domain", "f-action"].forEach(function (id) {
      $("#" + id).addEventListener("change", function () {
        filters.user = $("#f-user").value;
        filters.domain = $("#f-domain").value;
        filters.action = $("#f-action").value;
        repaint("activity");
      });
    });

    api("activity", { limit: 400 }).then(function (d) {
      State.events = d.events || [];
      rebuildUserFilter();
      repaint("activity");
    });
  }

  /* ------------------------------------------------- change inspector */

  function showEvent(e) {
    var back = el("div", "modal-backdrop");
    var modal = el("div", "modal");
    var head = el("header");
    head.appendChild(el("h3", null, "Change details"));
    var close = el("button", null, "Close");
    close.addEventListener("click", function () { back.remove(); });
    head.appendChild(close);
    modal.appendChild(head);

    var body = el("div", "content");
    function line(k, v, mono) {
      var row = el("div", "summary-line");
      row.appendChild(el("div", "k", k));
      var val = el("div", "v" + (v === null || v === undefined || v === "" ? " na" : ""));
      val.textContent = (v === null || v === undefined || v === "") ? "Not available" : v;
      if (mono) { val.style.fontFamily = "var(--mono)"; }
      row.appendChild(val);
      body.appendChild(row);
    }

    line("User", e.username);
    line("Time", new Date(e.ts * 1000).toLocaleString());
    line("Domain", e.domain === "schematic" ? "Schematic"
         : e.domain === "pcb" ? "PCB" : e.domain);
    line("Object", e.object_ref || e.object_id);
    line("Object type", e.object_type);
    line("Action", e.action);
    line("Description", e.description);
    if (e.field) { line("Property", e.field); }

    if (e.old_value !== null && e.old_value !== undefined) {
      line("Before", formatValue(e.old_value), true);
    }
    if (e.new_value !== null && e.new_value !== undefined) {
      line("After", formatValue(e.new_value), true);
    }
    if (e.version) { line("Version", "v" + e.version); }

    modal.appendChild(body);
    back.appendChild(modal);
    back.addEventListener("click", function (ev) { if (ev.target === back) { back.remove(); } });
    document.body.appendChild(back);
  }

  function formatValue(v) {
    if (v === null || v === undefined) { return ""; }
    if (typeof v === "object") {
      if ("x" in v && "y" in v) { return "X = " + v.x + "   Y = " + v.y; }
      return JSON.stringify(v);
    }
    return String(v);
  }

  /* ------------------------------------------------------------- comments */

  function pageComments() {
    var filter = "all";

    painters.comments = function () {
      var host = $("#threads");
      clear(host);
      var rows = State.threads.filter(function (t) {
        if (filter === "open") { return t.status !== "resolved"; }
        if (filter === "resolved") { return t.status === "resolved"; }
        if (filter === "mine") { return t.is_mine; }
        return true;
      });
      $("#c-count").textContent = rows.length + " thread" + (rows.length === 1 ? "" : "s");
      if (!rows.length) {
        host.appendChild(el("p", "empty", "No comments here yet."));
        return;
      }
      rows.forEach(function (t) { host.appendChild(threadCard(t)); });
    };

    $("#c-filter").addEventListener("change", function () {
      filter = this.value; repaint("comments");
    });

    $("#c-new").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var ref = $("#c-object").value.trim();
      var text = $("#c-text").value.trim();
      if (!ref || !text) { return; }
      var target = resolveTarget(ref);
      if (!target) {
        toast("No object called \"" + ref + "\" in this project", "conflict");
        return;
      }
      send({ type: "comment_create", domain: target.domain, object_id: target.object_id,
             object_type: target.object_type, object_ref: target.object_ref,
             text: text });
      $("#c-text").value = "";
    });

    loadTargets();
    refreshComments();
  }

  var TARGETS = [];

  function loadTargets() {
    Promise.all([
      api("schematic").catch(function () { return {}; }),
      api("pcb").catch(function () { return {}; })
    ]).then(function (res) {
      TARGETS = [];
      (res[0].components || []).forEach(function (c) {
        TARGETS.push({ domain: "schematic", object_type: "symbol",
                       object_ref: c.reference, object_id: "sch:" + c.reference,
                       label: c.reference + "  " + (c.value || "") + "  (schematic)" });
      });
      (res[1].objects || []).forEach(function (o) {
        TARGETS.push({ domain: "pcb", object_type: "footprint",
                       object_ref: o.reference, object_id: o.uuid,
                       label: o.reference + "  (PCB)" });
      });
      var list = $("#object-list");
      if (list) {
        clear(list);
        TARGETS.forEach(function (t) {
          var o = document.createElement("option");
          o.value = t.object_ref;
          o.label = t.label;
          list.appendChild(o);
        });
      }
    });
  }

  function resolveTarget(reference) {
    var domain = ($("#c-domain") && $("#c-domain").value) || "schematic";
    var exact = TARGETS.filter(function (t) {
      return t.object_ref === reference && t.domain === domain;
    });
    if (exact.length) { return exact[0]; }
    var any = TARGETS.filter(function (t) { return t.object_ref === reference; });
    return any.length ? any[0] : null;
  }

  function threadCard(t) {
    var card = el("div", "thread" + (t.status === "resolved" ? " resolved" : ""));

    var head = el("div", "thread-head");
    head.appendChild(domainChip(t.domain));
    head.appendChild(el("span", "chip", t.object_ref || t.object_id.slice(0, 10)));
    if (t.sheet) { head.appendChild(el("span", "meta", t.sheet)); }
    head.appendChild(el("span", "status " + (t.status === "resolved" ? "resolved" : "open"),
                        t.status));
    card.appendChild(head);

    card.appendChild(messageRow(t, false));
    (t.replies || []).forEach(function (r) { card.appendChild(messageRow(r, true)); });

    var actions = el("div", "thread-actions");
    var replyBox = el("input");
    replyBox.type = "text";
    replyBox.placeholder = "Reply…";
    replyBox.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && replyBox.value.trim()) {
        send({ type: "comment_create", domain: t.domain, object_id: t.object_id,
               object_type: t.object_type, object_ref: t.object_ref,
               parent_id: t.comment_id, text: replyBox.value.trim() });
        replyBox.value = "";
      }
    });
    actions.appendChild(replyBox);

    var toggle = el("button", t.status === "resolved" ? "" : "primary",
                    t.status === "resolved" ? "Reopen" : "Resolve");
    toggle.addEventListener("click", function () {
      send({ type: "comment_status", comment_id: t.comment_id,
             status: t.status === "resolved" ? "reopened" : "resolved" });
    });
    actions.appendChild(toggle);
    card.appendChild(actions);
    return card;
  }

  function messageRow(c, isReply) {
    var row = el("div", "msg" + (isReply ? " reply" : ""));
    row.appendChild(avatar(c.author_name, c.author_id, isReply));
    var body = el("div", "body");
    var head = el("div", "head");
    var name = el("span", "name", c.author_name);
    name.style.color = colourFor(c.author_id);
    head.appendChild(name);
    head.appendChild(el("span", "when", clockOf(c.created_at) + " · " + agoOf(c.created_at)));
    body.appendChild(head);
    body.appendChild(el("div", "text", c.text));
    row.appendChild(body);
    return row;
  }

  /* -------------------------------------------------------------- project */

  function pageProject() {
    var host = $("#summary");
    api("summary").then(function (d) {
      State.summary = d;
      clear(host);

      if (!d.available) {
        host.appendChild(el("p", "empty", d.reason || "No project analysed."));
        return;
      }

      (d.headline || []).forEach(function (line) {
        var row = el("div", "summary-line");
        row.appendChild(el("div", "k", line.label));
        var v = el("div", "v" + (line.value === null ? " na" : ""));
        v.textContent = line.value === null ? "Not available" : line.value;
        if (line.evidence) { v.appendChild(el("small", "ev", line.evidence)); }
        row.appendChild(v);
        host.appendChild(row);
      });

      $("#gen-meta").textContent =
        "Generated locally in " + d.elapsed_ms + " ms · no internet connection used";

      var sch = d.schematic;
      if (sch) {
        var inv = $("#inventory");
        clear(inv);
        sch.components.inventory.forEach(function (row) {
          var tr = el("tr");
          tr.appendChild(el("td", null, row.category));
          tr.appendChild(el("td", "mono", String(row.count)));
          tr.appendChild(el("td", "mono", row.references.slice(0, 10).join(", ")));
          inv.appendChild(tr);
        });

        var ifs = $("#interfaces");
        clear(ifs);
        if (!sch.interfaces.length) {
          ifs.appendChild(el("li", "empty",
            "No interfaces detected. Nothing in the schematic names one."));
        }
        sch.interfaces.forEach(function (i) {
          var li = el("li");
          li.appendChild(el("span", "chip", i.name));
          li.appendChild(el("span", "name", i.confidence));
          var ev = el("span", "meta", i.evidence[0] || "");
          ev.style.fontSize = "11px";
          li.appendChild(ev);
          ifs.appendChild(li);
        });

        var pw = $("#power");
        clear(pw);
        var rails = (sch.power.rails || []).concat(sch.power.grounds || []);
        if (!rails.length) { pw.appendChild(el("li", "empty", "No power nets identified.")); }
        rails.forEach(function (r) {
          var li = el("li");
          li.appendChild(el("span", "chip", r.leaf || r.name));
          li.appendChild(el("span", "meta", r.nodes + " connections"));
          pw.appendChild(li);
        });
      }

      var pcb = d.pcb;
      var pcbHost = $("#board");
      clear(pcbHost);
      if (!pcb) {
        pcbHost.appendChild(el("p", "empty", "No board analysed."));
      } else {
        [["Layers", (pcb.copper_layers || []).join(", ")],
         ["Footprints", pcb.footprint_count],
         ["Nets", pcb.net_count],
         ["Track segments", pcb.track_segment_count],
         ["Vias", pcb.via_count],
         ["Zones", pcb.zone_count],
         ["Size (mm)", pcb.dimensions_mm
            ? pcb.dimensions_mm.width + " × " + pcb.dimensions_mm.height : null]
        ].forEach(function (pair) {
          var row = el("div", "summary-line");
          row.appendChild(el("div", "k", pair[0]));
          var v = el("div", "v" + (pair[1] === null || pair[1] === undefined ? " na" : ""));
          v.textContent = (pair[1] === null || pair[1] === undefined) ? "Not available" : pair[1];
          row.appendChild(v);
          pcbHost.appendChild(row);
        });
      }

      if ((d.warnings || []).length) {
        var w = $("#warnings");
        clear(w);
        d.warnings.forEach(function (msg) { w.appendChild(el("li", null, msg)); });
        $("#warn-card").style.display = "";
      }
    }).catch(function (e) {
      clear(host);
      host.appendChild(el("p", "empty", "Summary unavailable: " + e.message));
    });
  }

  /* ------------------------------------------------------- schematic/pcb */

  function pageDomain(domain) {
    var endpoint = domain === "schematic" ? "schematic" : "pcb";

    api(endpoint).then(function (d) {
      var host = $("#objects");
      clear(host);

      if (domain === "schematic" && !d.available) {
        host.appendChild(el("tr")).appendChild(el("td", "empty",
          d.reason || "No schematic found for this project."));
        return;
      }

      var rows = domain === "schematic" ? (d.components || []) : (d.objects || []);
      if (!rows.length) {
        var tr = el("tr");
        tr.appendChild(el("td", "empty", "Nothing to show yet."));
        host.appendChild(tr);
        return;
      }

      var lockBy = {};
      (d.locks || []).forEach(function (lk) { lockBy[lk.reference] = lk.owner_name; });
      var commentOn = {};
      Object.keys(d.comments || {}).forEach(function (k) {
        var c = d.comments[k];
        if (c.reference) { commentOn[c.reference] = c; }
      });

      rows.forEach(function (o) {
        var tr = el("tr", "clickable");
        tr.appendChild(el("td", "mono", o.reference || "—"));
        tr.appendChild(el("td", null, o.value || ""));
        tr.appendChild(el("td", "mono",
          domain === "schematic" ? (o.sheet || "/")
            : (o.position ? o.position.x + ", " + o.position.y : "")));
        tr.appendChild(el("td", null,
          domain === "schematic" ? (o.footprint || "") : (o.layer || "")));

        var stat = el("td");
        if (lockBy[o.reference]) {
          stat.appendChild(el("span", "chip lock", "locked by " + lockBy[o.reference]));
        }
        var cm = commentOn[o.reference];
        if (cm && cm.open) { stat.appendChild(el("span", "chip", cm.open + " open")); }
        tr.appendChild(stat);

        tr.addEventListener("click", function () { openCommentFor(o, domain); });
        host.appendChild(tr);
      });

      $("#obj-count").textContent = rows.length + " object" + (rows.length === 1 ? "" : "s");
    });
  }

  function openCommentFor(obj, domain) {
    var back = el("div", "modal-backdrop");
    var modal = el("div", "modal");
    var head = el("header");
    head.appendChild(el("h3", null, "Comment on " + (obj.reference || "object")));
    var close = el("button", null, "Close");
    close.addEventListener("click", function () { back.remove(); });
    head.appendChild(close);
    modal.appendChild(head);

    var body = el("div", "content");
    var area = el("textarea");
    area.placeholder = "What needs checking on " + (obj.reference || "this object") + "?";
    body.appendChild(area);
    var submit = el("button", "primary", "Post comment");
    submit.style.marginTop = "10px";
    submit.addEventListener("click", function () {
      var text = area.value.trim();
      if (!text) { return; }
      send({ type: "comment_create", domain: domain,
             object_id: domain === "schematic" ? "sch:" + obj.reference : obj.uuid,
             object_type: domain === "schematic" ? "symbol" : "footprint",
             object_ref: obj.reference, sheet: obj.sheet || null, text: text });
      back.remove();
      toast("Comment posted on " + obj.reference);
    });
    body.appendChild(submit);
    modal.appendChild(body);
    back.appendChild(modal);
    back.addEventListener("click", function (ev) { if (ev.target === back) { back.remove(); } });
    document.body.appendChild(back);
  }

  /* ----------------------------------------------------------------- boot */

  function boot() {
    renderShell();
    renderTop();
    connect();

    switch (document.body.dataset.page) {
      case "index": pageOverview(); break;
      case "activity": pageActivity(); break;
      case "comments": pageComments(); break;
      case "project": pageProject(); break;
      case "schematic": pageDomain("schematic"); break;
      case "pcb": pageDomain("pcb"); break;
      default: break;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }
})();
