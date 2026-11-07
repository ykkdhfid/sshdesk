/* SSHDesk Web 前端：登录 → 服务器列表 → xterm 终端 → 系统状态浮层 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);

  const state = {
    servers: [],
    server: null,
    term: null,
    fit: null,
    ws: null,
    infoTimer: null,
  };

  /* ---------------------------------------------------------------- 基础 */
  function setView(name) {
    ["login-view", "list-view", "term-view"].forEach((id) => {
      $(id).classList.toggle("hidden", id !== name + "-view");
    });
  }

  let toastTimer = null;
  function toast(message, ms = 2600) {
    const el = $("toast");
    el.textContent = message;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...options });
    if (!res.ok) {
      let message = "请求失败";
      try {
        const body = await res.json();
        message = body.detail || message;
      } catch (e) { /* 非 JSON 响应 */ }
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function setConnState(text, cls) {
    const el = $("conn-state");
    el.textContent = text;
    el.className = "state" + (cls ? " " + cls : "");
  }

  /* ---------------------------------------------------------------- 登录 */
  async function boot() {
    try {
      state.servers = await api("/api/servers");
      renderList();
      setView("list");
    } catch (e) {
      setView("login");
    }
    adaptViewport();
  }

  async function login() {
    const token = $("token-input").value.trim();
    if (!token) {
      $("login-error").textContent = "请先填写访问令牌";
      return;
    }
    try {
      await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      state.servers = await api("/api/servers");
      renderList();
      $("login-error").textContent = "";
      setView("list");
    } catch (e) {
      $("login-error").textContent =
        e.status === 429 ? "尝试次数过多，请稍后再试" : "令牌不正确，请核对后重试";
    }
  }

  /* ---------------------------------------------------------------- 列表 */
  function renderList() {
    const list = $("server-list");
    list.innerHTML = "";
    $("server-count").textContent = state.servers.length + " 台";
    $("list-empty").classList.toggle("hidden", state.servers.length > 0);

    state.servers.forEach((server) => {
      const card = document.createElement("div");
      card.className = "server-card";

      const dot = document.createElement("span");
      dot.className = "dot";

      const main = document.createElement("div");
      main.className = "server-main";

      const name = document.createElement("div");
      name.className = "server-name";
      name.textContent = server.name;

      const addr = document.createElement("div");
      addr.className = "server-addr";
      addr.textContent = server.username + "@" + server.host + ":" + server.port;

      const meta = document.createElement("div");
      meta.className = "server-meta";
      const parts = [server.group || "默认", server.auth];
      if (server.last_connected_at) {
        parts.push("上次 " + new Date(server.last_connected_at * 1000).toLocaleDateString());
      }
      meta.textContent = parts.join(" · ");

      const chev = document.createElement("span");
      chev.className = "chev";
      chev.textContent = "›";

      main.append(name, addr, meta);
      card.append(dot, main, chev);
      card.addEventListener("click", () => openTerminal(server));
      list.appendChild(card);
    });
  }

  /* ---------------------------------------------------------------- 终端 */
  function ensureTerminal() {
    if (state.term) return;
    state.term = new Terminal({
      fontFamily:
        '"JetBrains Mono", "Cascadia Mono", "SF Mono", Menlo, Consolas, "Noto Sans Mono CJK SC", monospace',
      fontSize: isMobile ? 12 : 14,
      lineHeight: 1.25,
      cursorBlink: true,
      scrollback: 5000,
      convertEol: false,
      allowProposedApi: false,
      theme: {
        background: "#12101b",
        foreground: "#e9e4f7",
        cursor: "#a78bfa",
        cursorAccent: "#12101b",
        selectionBackground: "#3a2f5e",
        black: "#3b3653", red: "#f8748f", green: "#62d6a6", yellow: "#e8c170",
        blue: "#5aa2ff", magenta: "#c4a5ff", cyan: "#79d2e6", white: "#e9e4f7",
        brightBlack: "#5a5478", brightRed: "#ff93a7", brightGreen: "#7fe4bb",
        brightYellow: "#f4d59a", brightBlue: "#7ab8ff", brightMagenta: "#d8bfff",
        brightCyan: "#9fe0f0", brightWhite: "#ffffff",
      },
    });
    state.fit = new FitAddon.FitAddon();
    state.term.loadAddon(state.fit);
    state.term.open($("terminal"));

    state.term.onData((data) => {
      if (state.ws && state.ws.readyState === 1) {
        state.ws.send(JSON.stringify({ type: "input", data }));
      }
    });
    state.term.onResize(({ cols, rows }) => {
      if (state.ws && state.ws.readyState === 1) {
        state.ws.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    });
  }

  function fitNow() {
    if (!state.fit) return;
    try {
      state.fit.fit();
    } catch (e) { /* 容器不可见时会失败，忽略 */ }
  }

  function sendResize() {
    if (state.ws && state.ws.readyState === 1 && state.term) {
      state.ws.send(JSON.stringify({ type: "resize", cols: state.term.cols, rows: state.term.rows }));
    }
  }

  function openTerminal(server) {
    state.server = server;
    $("term-name").textContent = server.name;
    $("reconnect-btn").classList.add("hidden");
    $("sysinfo").classList.add("hidden");
    setView("term");
    ensureTerminal();
    state.term.reset();
    requestAnimationFrame(() => {
      fitNow();
      state.term.focus();
    });
    connect();
  }

  function connect() {
    if (state.ws) {
      try { state.ws.close(); } catch (e) { /* 忽略 */ }
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url =
      scheme + "://" + location.host + "/ws/term/" + encodeURIComponent(state.server.id);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    setConnState("连接中…", "");
    $("reconnect-btn").classList.add("hidden");

    ws.onopen = () => {
      // WS 通了不代表 SSH 通了，还要等 ready 帧
      setConnState("握手中…", "");
      fitNow();
      // 服务端 open_shell 时用的是默认 100x30，这里必须把真实尺寸发过去，
      // 否则远端按 100 列排版，窄屏上会整段错行。
      sendResize();
      state.term.focus();
    };

    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        let msg = {};
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        if (msg.type === "ready") {
          setConnState("已连接", "ok");
          if (msg.name) $("term-name").textContent = msg.name;
          setTimeout(() => { fitNow(); sendResize(); }, 80);
        } else if (msg.type === "error") {
          setConnState("连接失败", "error");
          const text = msg.message + (msg.hint ? "\n" + msg.hint : "");
          state.term.write("\r\n\x1b[31m" + text.replace(/\n/g, "\r\n") + "\x1b[0m\r\n");
          toast(msg.message);
          $("reconnect-btn").classList.remove("hidden");
        } else if (msg.type === "closed") {
          setConnState("已断开", "error");
          $("reconnect-btn").classList.remove("hidden");
        }
        return;
      }
      state.term.write(new Uint8Array(event.data));
    };

    ws.onclose = (event) => {
      if (state.ws !== ws) return;
      if (event.code === 4401) {
        setConnState("登录已失效", "error");
        setView("login");
        return;
      }
      setConnState("已断开", "error");
      $("reconnect-btn").classList.remove("hidden");
    };

    ws.onerror = () => setConnState("连接异常", "error");
  }

  function backToList() {
    if (state.ws) {
      try { state.ws.close(); } catch (e) { /* 忽略 */ }
      state.ws = null;
    }
    stopInfo();
    $("sysinfo").classList.add("hidden");
    setView("list");
  }

  /* ---------------------------------------------------------------- 状态 */
  function barRow(label, percent) {
    const wrap = document.createElement("div");
    wrap.className = "bar-row";
    const name = document.createElement("span");
    name.className = "bar-label";
    name.textContent = label;

    const bar = document.createElement("div");
    bar.className = "bar" + (percent >= 90 ? " danger" : percent >= 75 ? " warn" : "");
    const fill = document.createElement("i");
    fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
    bar.appendChild(fill);

    const value = document.createElement("span");
    value.className = "bar-value";
    value.textContent = percent.toFixed(0) + "%";

    wrap.append(name, bar, value);
    return wrap;
  }

  function kv(label, value) {
    const row = document.createElement("div");
    row.className = "kv";
    const k = document.createElement("span");
    k.textContent = label;
    const v = document.createElement("b");
    v.textContent = value;
    row.append(k, v);
    return row;
  }

  async function loadInfo() {
    if (!state.server) return;
    const body = $("sysinfo-body");
    try {
      const data = await api("/api/monitor/" + encodeURIComponent(state.server.id));
      body.innerHTML = "";
      body.append(
        barRow("CPU", data.cpu),
        barRow("内存", data.mem_percent),
        barRow("磁盘", data.disk_percent),
        kv("负载", (data.load || []).map((n) => n.toFixed(2)).join(" / ")),
        kv("内存用量", data.mem_text),
        kv("磁盘用量", data.disk_text),
        kv("运行时长", data.uptime),
        kv("系统", (data.os || "") + " · " + (data.kernel || "")),
        kv("sshd", data.ssh || "未知")
      );
      const procs = (data.processes || []).slice(0, 4);
      if (procs.length) {
        const title = document.createElement("div");
        title.className = "kv";
        title.innerHTML = "<span>TOP 进程</span><b></b>";
        body.append(title);
        procs.forEach((p) => kv("· " + p.cmd, p.cpu.toFixed(1) + "% / " + p.mem.toFixed(1) + "%"));
      }
    } catch (e) {
      body.textContent = "读取失败：" + e.message;
    }
  }

  function openInfo() {
    $("sysinfo").classList.remove("hidden");
    loadInfo();
    if (!state.infoTimer) state.infoTimer = setInterval(loadInfo, 5000);
  }

  function stopInfo() {
    if (state.infoTimer) {
      clearInterval(state.infoTimer);
      state.infoTimer = null;
    }
  }

  /* ---------------------------------------------------------------- 视口 */
  function adaptViewport() {
    const apply = () => {
      const height = (window.visualViewport && window.visualViewport.height) || window.innerHeight;
      document.documentElement.style.setProperty("--vh", height + "px");
    };
    apply();
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", () => {
        apply();
        setTimeout(fitNow, 120);
      });
    }
    window.addEventListener("resize", () => {
      apply();
      setTimeout(fitNow, 120);
    });
    window.addEventListener("orientationchange", () => setTimeout(() => { apply(); fitNow(); }, 300));
  }

  /* ---------------------------------------------------------------- 绑定 */
  $("login-btn").addEventListener("click", login);
  $("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
  });

  $("logout-btn").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
    setView("login");
  });

  $("back-btn").addEventListener("click", backToList);
  $("reconnect-btn").addEventListener("click", () => {
    state.term.reset();
    connect();
  });
  $("info-btn").addEventListener("click", openInfo);
  $("close-info").addEventListener("click", () => {
    $("sysinfo").classList.add("hidden");
    stopInfo();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopInfo();
  });

  boot();
})();
