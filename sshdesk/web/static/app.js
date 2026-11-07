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
  /* 星点：每颗是独立元素，位置随机、相位随机，所以是"一闪一闪"而不是整片呼吸。
     落点要躲开输入栏和内容块——星点压在 token 输入框上会让人以为屏幕有坏点。 */
  const STAR_AVOID = "input, textarea, button, .login-box, .server-card, .term-head, .tools";

  function avoidRects() {
    return [...document.querySelectorAll(STAR_AVOID)]
      .map((el) => el.getBoundingClientRect())
      .filter((r) => r.width > 0 && r.height > 0);
  }

  function initStars() {
    const W = window.innerWidth;
    const H = window.innerHeight;
    const rects = avoidRects();
    const total = W < 600 ? 18 : 26;

    document.querySelectorAll(".starfield").forEach((view) => {
      view.querySelector(".star-layer")?.remove();
      const layer = document.createElement("div");
      layer.className = "star-layer";

      for (let i = 0; i < total; i++) {
        let x = 0;
        let y = 0;
        let ok = false;
        for (let tries = 0; tries < 40 && !ok; tries++) {
          x = Math.random() * W;
          y = Math.random() * H;
          ok =
            y > 64 && // 顶栏下面才开始有星
            !rects.some(
              (r) => x > r.left - 26 && x < r.right + 26 && y > r.top - 26 && y < r.bottom + 26,
            );
        }
        if (!ok) continue;

        const star = document.createElement("div");
        const rare = Math.random() < 0.22; // 约五分之一是"偶尔才出现"的
        star.className = rare ? "star rare" : "star";
        star.style.left = `${((x / W) * 100).toFixed(2)}%`;
        star.style.top = `${((y / H) * 100).toFixed(2)}%`;
        star.style.setProperty("--o", (0.3 + Math.random() * 0.55).toFixed(2));
        star.style.setProperty(
          "--dur",
          `${(rare ? 14 + Math.random() * 10 : 3.4 + Math.random() * 4.2).toFixed(2)}s`,
        );
        star.style.setProperty("--delay", (Math.random() * 8).toFixed(2) + "s");
        layer.appendChild(star);
      }

      view.appendChild(layer);
    });
  }

  async function boot() {
    initStars();
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
    const btn = $("login-btn");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "验证中…";
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
    } finally {
      btn.disabled = false;
      btn.textContent = label;
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
      // 宽屏上字符放大，别让 1440 的终端挤成蚂蚁字
      fontSize: isMobile ? 12 : (window.innerWidth >= 1200 ? 16 : 14),
      lineHeight: 1.25,
      cursorBlink: true,
      scrollback: 5000,
      convertEol: false,
      allowProposedApi: false,
      theme: {
        background: "#04060d",
        foreground: "#e6eaf5",
        cursor: "#8fb0f5",
        cursorAccent: "#04060d",
        selectionBackground: "#1d2b4a",
        black: "#243049", red: "#f0708a", green: "#5fd1a0", yellow: "#e0b76a",
        blue: "#7fa6ff", magenta: "#a9a0e8", cyan: "#69c8d8", white: "#e6eaf5",
        brightBlack: "#5c6a86", brightRed: "#ff8fa3", brightGreen: "#7fdfb4",
        brightYellow: "#f0cd92", brightBlue: "#9dbcff", brightMagenta: "#c4bbff",
        brightCyan: "#8fdce9", brightWhite: "#ffffff",
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
    let starTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(starTimer);
    starTimer = setTimeout(initStars, 300);
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
