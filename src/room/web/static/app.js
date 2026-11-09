// The writers room page scripts: follow a live log, and talk to a writer.
(() => {
  const $ = (s, el = document) => el.querySelector(s);

  // ── live activity log ──────────────────────────────────────────────────────────────────────
  const log = $("#log");
  if (log) {
    const pitch = $(".pitch");
    let follow = log.dataset.follow === "1";
    const pull = async () => {
      try {
        const r = await fetch(log.dataset.src);
        const text = await r.text();
        if (text !== log.textContent) {
          const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
          log.textContent = text || "Starting…";
          if (atBottom) log.scrollTop = log.scrollHeight;
          log.closest("section")?.removeAttribute("hidden");
        }
      } catch (_) { /* the server restarted; try again next tick */ }
    };
    pull();
    // While something runs, poll the log and the pitch's state; reload when the work ends so
    // the brief, review and draft appear.
    setInterval(async () => {
      if (!follow) return;
      pull();
      if (pitch) {
        try {
          const s = await (await fetch(`/pitches/${pitch.dataset.pitch}/state`)).json();
          if (!s.busy || s.status !== pitch.dataset.status) location.reload();
        } catch (_) {}
      }
    }, 2500);
    // The scout on the board: reload when it has finished, so its pitches show.
    if (!pitch && follow) setTimeout(function check() {
      fetch("/").then(r => r.text()).then(html => {
        if (!html.includes("Scouting…")) location.reload(); else setTimeout(check, 4000);
      });
    }, 4000);
  }

  // ── chat ───────────────────────────────────────────────────────────────────────────────────
  const chat = $(".chat");
  if (!chat) return;
  const list = $("#messages");
  const form = $("#composer");
  const box = $("#text");
  const first = chat.dataset.first;
  const scroll = () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  scroll();

  const add = (who, text) => {
    const li = document.createElement("li");
    li.className = `msg ${who}`;
    const b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    li.appendChild(b);
    list.appendChild(li);
    scroll();
    return { li, b };
  };

  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = box.value.trim();
    if (!text) return;
    box.value = "";
    box.disabled = true;
    $(".intro-msg")?.remove();
    add("you", text);
    const { li, b } = add("writer", "");
    li.classList.add("typing");
    const status = document.createElement("div");
    status.className = "tool-status";
    status.textContent = `${first} is thinking…`;
    li.appendChild(status);
    let reply = "";
    try {
      const r = await fetch(`/chats/${chat.dataset.chat}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const ev = JSON.parse(buf.slice(0, nl));
          buf = buf.slice(nl + 1);
          if (ev.type === "delta") { reply += ev.text; b.textContent = reply; status.textContent = ""; scroll(); }
          else if (ev.type === "restart") { reply = ""; }
          else if (ev.type === "tool") { status.textContent = `${first} is ${ev.label}…`; }
          else if (ev.type === "done") { status.textContent = ""; }
        }
      }
      location.reload(); // the server's copy has the formatted reply and the pin buttons
    } catch (err) {
      li.classList.remove("typing");
      status.textContent = `Couldn't reach ${first}: ${err.message}`;
      box.disabled = false;
    }
  });

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest(".pin");
    if (!btn) return;
    const r = await fetch(`/messages/${btn.dataset.id}/pin`, { method: "POST" });
    if (!r.ok) return;
    const { pinned } = await r.json();
    btn.textContent = pinned ? "Pinned as direction" : "Pin as direction";
    btn.closest(".msg").classList.toggle("pinned", pinned);
  });
})();
