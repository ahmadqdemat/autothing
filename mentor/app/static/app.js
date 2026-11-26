let sessionId = null;
let currentImageB64 = null;
let currentImagePreview = null;
let isSending = false;
let conversations = [];
let parentToken = null;
let parentCurrentId = null;

const STORAGE_SESSION_KEY = "mentor_current_session";
const STORAGE_SIDEBAR_KEY = "mentor_sidebar_collapsed";
const STORAGE_THEME_KEY = "mentor_theme";
const STORAGE_VOICE_LANG = "mentor_voice_lang";
const UNTITLED_LABEL = "محادثة جديدة";
const MAX_IMAGE_SIDE = 1280;

const getApiUrl = (endpoint) => window.location.pathname.replace(/\/$/, "") + endpoint;

const apiHeaders = (json = false) => {
  const headers = { "X-Session-ID": sessionId || "" };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
};

const imageUrl = (convId, name) =>
  getApiUrl("/api/images/" + encodeURIComponent(convId) + "/" + encodeURIComponent(name));

const isMobileView = () => window.matchMedia("(max-width: 768px)").matches;

document.addEventListener("DOMContentLoaded", () => {
  const app = document.getElementById("app");
  const chatForm = document.getElementById("chat-form");
  const userInput = document.getElementById("user-input");
  const sendBtn = document.getElementById("send-btn");
  const unlockBtn = document.getElementById("unlock-btn");
  const modal = document.getElementById("unlock-modal");
  const modalSubmit = document.getElementById("modal-submit-btn");

  if (isMobileView() || localStorage.getItem(STORAGE_SIDEBAR_KEY) === "true") {
    app.classList.add("sidebar-collapsed");
  }
  const setSidebarCollapsed = (collapsed) => {
    app.classList.toggle("sidebar-collapsed", collapsed);
    if (!isMobileView()) localStorage.setItem(STORAGE_SIDEBAR_KEY, String(collapsed));
  };
  document.getElementById("toggle-sidebar-btn").addEventListener("click", () => setSidebarCollapsed(false));
  document.getElementById("close-sidebar-btn").addEventListener("click", () => setSidebarCollapsed(true));
  document.getElementById("sidebar-backdrop").addEventListener("click", () => setSidebarCollapsed(true));
  window.setSidebarCollapsed = setSidebarCollapsed;

  // Keep the app exactly the size of the visible screen, so the on-screen keyboard
  // can't push the top bar out of view or leave empty space after it closes.
  const fitViewport = () => {
    const vv = window.visualViewport;
    const h = vv ? vv.height : window.innerHeight;
    document.documentElement.style.setProperty("--app-h", Math.round(h) + "px");
    if (window.scrollY || document.documentElement.scrollTop || document.body.scrollTop) {
      window.scrollTo(0, 0);
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    }
  };
  fitViewport();
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitViewport);
    window.visualViewport.addEventListener("scroll", fitViewport);
  }
  window.addEventListener("resize", fitViewport);
  window.addEventListener("orientationchange", () => setTimeout(fitViewport, 300));

  applyTheme(document.documentElement.getAttribute("data-theme") || "light", false);
  document.getElementById("theme-toggle-btn").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    applyTheme(current === "dark" ? "light" : "dark", true);
  });

  const refreshComposerState = () => {
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 200) + "px";
    sendBtn.disabled = isSending || (!userInput.value.trim() && !currentImageB64);
  };
  userInput.addEventListener("input", refreshComposerState);
  userInput.addEventListener("blur", () => setTimeout(fitViewport, 150));
  window.refreshComposerState = refreshComposerState;
  const isTouch = window.matchMedia("(pointer: coarse)").matches;
  const focusInput = () => { if (!isTouch) userInput.focus(); };

  userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      chatForm.requestSubmit();
    }
  });

  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (isSending) return;
    const message = userInput.value.trim();
    if (!message && !currentImageB64) return;
    stopVoice();

    const isCommand = /^\/(un)?lock\b/i.test(message);
    hideWelcome();
    if (!isCommand) appendMessage("user", message, currentImagePreview);
    userInput.value = "";
    const imgPayload = currentImageB64;
    clearImagePreview();

    isSending = true;
    refreshComposerState();
    const typingEl = showTyping();
    let bubble = null;

    try {
      const res = await fetch(getApiUrl("/api/chat"), {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ message: message, image_b64: imgPayload })
      });
      if (!res.ok || !res.body) {
        let detail = "خطأ في الاتصال بالسيرفر.";
        try { detail = (await res.json()).detail || detail; } catch (err) {}
        typingEl.remove();
        appendMessage("assistant", detail);
        return;
      }
      await readStream(res, (evt) => {
        if (evt.type === "meta") {
          setSessionId(evt.session_id);
        } else if (evt.type === "delta") {
          if (!bubble) {
            typingEl.remove();
            bubble = appendMessage("assistant", "", null, { streaming: true });
          }
          bubble.text += evt.text;
          bubble.render();
        } else if (evt.type === "done") {
          updateUIState(evt.is_full_mode, evt.current_rung);
        }
      });
      if (!bubble) {
        typingEl.remove();
      } else {
        bubble.finish();
      }
      loadConversations();
    } catch (err) {
      typingEl.remove();
      if (bubble) bubble.finish();
      appendMessage("assistant", "خطأ في الاتصال بالسيرفر.");
    } finally {
      isSending = false;
      refreshComposerState();
      focusInput();
    }
  });

  const onPick = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const dataUrl = await shrinkImage(file);
      currentImagePreview = dataUrl;
      currentImageB64 = dataUrl.split(",")[1];
      document.getElementById("image-preview").src = dataUrl;
      document.getElementById("image-preview-container").classList.remove("hidden");
    } catch (err) {
      appendMessage("assistant", "تعذر قراءة الصورة. جرّب صورة أخرى.");
    }
    refreshComposerState();
  };
  document.getElementById("image-input").addEventListener("change", onPick);
  document.getElementById("camera-input").addEventListener("change", onPick);

  document.getElementById("remove-image-btn").addEventListener("click", () => {
    clearImagePreview();
    refreshComposerState();
  });

  document.getElementById("new-chat-btn").addEventListener("click", async () => {
    if (isSending) return;
    await startNewChat();
    if (isMobileView()) setSidebarCollapsed(true);
    focusInput();
  });

  unlockBtn.addEventListener("click", () => {
    if (unlockBtn.getAttribute("data-full") === "true") {
      lockSession();
    } else {
      modal.classList.remove("hidden");
      document.getElementById("passphrase-input").focus();
    }
  });
  document.getElementById("modal-cancel-btn").addEventListener("click", () => modal.classList.add("hidden"));
  document.getElementById("passphrase-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); modalSubmit.click(); }
  });
  modalSubmit.addEventListener("click", async () => {
    const input = document.getElementById("passphrase-input");
    const errorEl = document.getElementById("modal-error");
    errorEl.textContent = "";
    try {
      const res = await fetch(getApiUrl("/api/unlock"), {
        method: "POST", headers: apiHeaders(true), body: JSON.stringify({ passphrase: input.value })
      });
      const data = await res.json();
      if (res.ok) {
        modal.classList.add("hidden");
        input.value = "";
        setSessionId(data.session_id);
        updateUIState(data.is_full_mode, data.current_rung);
      } else {
        errorEl.textContent = data.detail || "كلمة المرور غير صحيحة.";
      }
    } catch (err) {
      errorEl.textContent = "خطأ في الاتصال بالشبكة.";
    }
  });

  setupVoice(userInput, refreshComposerState);
  setupParent();

  sessionId = localStorage.getItem(STORAGE_SESSION_KEY) || null;
  (async () => {
    if (sessionId) {
      const ok = await openConversation(sessionId, { silent: true });
      if (!ok) setSessionId(null);
    }
    if (!sessionId) await checkStatus();
    await loadConversations();
  })();
});

/* ---------- networking ---------- */

async function readStream(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

function shrinkImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.naturalWidth * scale);
      canvas.height = Math.round(img.naturalHeight * scale);
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("bad image")); };
    img.src = url;
  });
}

function applyTheme(theme, persist) {
  document.documentElement.setAttribute("data-theme", theme);
  const label = document.getElementById("theme-toggle-label");
  if (label) label.textContent = theme === "dark" ? "الوضع الفاتح" : "الوضع الداكن";
  if (persist) localStorage.setItem(STORAGE_THEME_KEY, theme);
}

function setSessionId(id) {
  sessionId = id || null;
  if (sessionId) localStorage.setItem(STORAGE_SESSION_KEY, sessionId);
  else localStorage.removeItem(STORAGE_SESSION_KEY);
  renderHistory();
}

function clearImagePreview() {
  currentImageB64 = null;
  currentImagePreview = null;
  document.getElementById("image-preview-container").classList.add("hidden");
}

async function checkStatus() {
  try {
    const res = await fetch(getApiUrl("/api/status"), { headers: apiHeaders() });
    const data = await res.json();
    setSessionId(data.session_id);
    updateUIState(data.is_full_mode, data.current_rung);
    const v = document.getElementById("app-version");
    if (v && data.version) v.textContent = "v" + data.version;
  } catch (e) {}
}

async function lockSession() {
  try {
    const res = await fetch(getApiUrl("/api/lock"), { method: "POST", headers: apiHeaders() });
    const data = await res.json();
    updateUIState(data.is_full_mode, data.current_rung);
  } catch (e) {}
}

/* ---------- conversation history ---------- */

async function loadConversations() {
  try {
    const res = await fetch(getApiUrl("/api/conversations"));
    conversations = (await res.json()).conversations || [];
  } catch (e) {
    conversations = [];
  }
  renderHistory();
}

function renderHistory() {
  const list = document.getElementById("chat-history-list");
  if (!list) return;
  list.innerHTML = "";
  if (!conversations.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "لا توجد محادثات بعد";
    list.appendChild(empty);
    return;
  }
  conversations.forEach((conv) => {
    const item = document.createElement("div");
    item.className = "history-item" + (conv.id === sessionId ? " active" : "");
    const titleBtn = document.createElement("button");
    titleBtn.type = "button";
    titleBtn.className = "history-item-title";
    titleBtn.textContent = conv.title || UNTITLED_LABEL;
    titleBtn.title = conv.title || UNTITLED_LABEL;
    titleBtn.setAttribute("dir", "auto");
    titleBtn.addEventListener("click", () => {
      if (isSending || conv.id === sessionId) return;
      openConversation(conv.id);
      if (isMobileView()) window.setSidebarCollapsed(true);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "history-item-delete";
    delBtn.title = "حذف المحادثة";
    delBtn.setAttribute("aria-label", "حذف المحادثة");
    delBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>';
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteConversation(conv.id); });
    item.appendChild(titleBtn);
    item.appendChild(delBtn);
    list.appendChild(item);
  });
}

async function openConversation(id, { silent = false } = {}) {
  try {
    const res = await fetch(getApiUrl("/api/conversations/" + encodeURIComponent(id)));
    if (!res.ok) return false;
    const data = await res.json();
    setSessionId(data.id);
    renderMessages(data.id, data.messages || []);
    updateUIState(data.is_full_mode, data.current_rung);
    return true;
  } catch (e) {
    if (!silent) appendMessage("assistant", "خطأ في الاتصال بالسيرفر.");
    return false;
  }
}

async function startNewChat() {
  try {
    const res = await fetch(getApiUrl("/api/conversations"), { method: "POST" });
    const data = await res.json();
    setSessionId(data.id);
    renderMessages(data.id, []);
    updateUIState(data.is_full_mode, data.current_rung);
  } catch (e) {
    appendMessage("assistant", "خطأ في الاتصال بالسيرفر.");
  }
}

async function deleteConversation(id) {
  if (!window.confirm("هل تريد حذف هذه المحادثة؟")) return;
  try {
    await fetch(getApiUrl("/api/conversations/" + encodeURIComponent(id)), { method: "DELETE" });
  } catch (e) {}
  if (id === sessionId) await startNewChat();
  await loadConversations();
}

/* ---------- voice input ---------- */

let recognition = null;
let recognizing = false;

function stopVoice() {
  if (recognition && recognizing) recognition.stop();
}

function setupVoice(userInput, refresh) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const micBtn = document.getElementById("mic-btn");
  const langBtn = document.getElementById("voice-lang-btn");
  if (!SR || !window.isSecureContext) return;
  micBtn.classList.remove("hidden");
  langBtn.classList.remove("hidden");

  let lang = localStorage.getItem(STORAGE_VOICE_LANG) || "ar-SA";
  const showLang = () => { langBtn.textContent = lang === "ar-SA" ? "ع" : "EN"; };
  showLang();
  langBtn.addEventListener("click", () => {
    lang = lang === "ar-SA" ? "en-US" : "ar-SA";
    localStorage.setItem(STORAGE_VOICE_LANG, lang);
    showLang();
  });

  let base = "";
  micBtn.addEventListener("click", () => {
    if (recognizing) { recognition.stop(); return; }
    recognition = new SR();
    recognition.lang = lang;
    recognition.interimResults = true;
    recognition.continuous = true;
    base = userInput.value ? userInput.value.replace(/\s*$/, " ") : "";
    recognition.onstart = () => { recognizing = true; micBtn.classList.add("recording"); };
    recognition.onend = () => { recognizing = false; micBtn.classList.remove("recording"); refresh(); };
    recognition.onerror = () => { recognizing = false; micBtn.classList.remove("recording"); };
    recognition.onresult = (e) => {
      let finalText = "";
      let interim = "";
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      userInput.value = base + finalText + interim;
      refresh();
    };
    recognition.start();
  });
}

/* ---------- parent view ---------- */

function setupParent() {
  const modal = document.getElementById("parent-modal");
  const passInput = document.getElementById("parent-pass-input");
  const errorEl = document.getElementById("parent-error");

  document.getElementById("parent-btn").addEventListener("click", () => {
    modal.classList.remove("hidden");
    if (parentToken) loadParentList();
    else { showParentLogin(); passInput.focus(); }
  });
  document.getElementById("parent-close-btn").addEventListener("click", () => modal.classList.add("hidden"));
  passInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); document.getElementById("parent-login-btn").click(); }
  });
  document.getElementById("parent-login-btn").addEventListener("click", async () => {
    errorEl.textContent = "";
    try {
      const res = await fetch(getApiUrl("/api/parent/login"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passphrase: passInput.value })
      });
      const data = await res.json();
      if (!res.ok) { errorEl.textContent = data.detail || "كلمة المرور غير صحيحة."; return; }
      parentToken = data.token;
      passInput.value = "";
      loadParentList();
    } catch (e) {
      errorEl.textContent = "خطأ في الاتصال بالشبكة.";
    }
  });
  document.getElementById("parent-logout-btn").addEventListener("click", async () => {
    try { await parentFetch("/api/parent/logout", { method: "POST" }); } catch (e) {}
    parentToken = null;
    showParentLogin();
  });
  document.getElementById("parent-back-btn").addEventListener("click", loadParentList);
  document.getElementById("parent-delete-btn").addEventListener("click", async () => {
    if (!parentCurrentId || !window.confirm("هل تريد حذف هذه المحادثة نهائياً؟")) return;
    await parentFetch("/api/parent/conversations/" + encodeURIComponent(parentCurrentId), { method: "DELETE" });
    loadParentList();
    loadConversations();
  });
}

function showParentLogin() {
  document.getElementById("parent-login").classList.remove("hidden");
  document.getElementById("parent-body").classList.add("hidden");
}

async function parentFetch(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {}, { "X-Parent-Token": parentToken || "" });
  const res = await fetch(getApiUrl(path), opts);
  if (res.status === 401) {
    parentToken = null;
    showParentLogin();
    throw new Error("auth");
  }
  return res;
}

function formatDate(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleString("ar", { dateStyle: "medium", timeStyle: "short" });
  } catch (e) {
    return new Date(ts * 1000).toLocaleString();
  }
}

async function loadParentList() {
  let data;
  try {
    data = await (await parentFetch("/api/parent/conversations")).json();
  } catch (e) { return; }
  document.getElementById("parent-login").classList.add("hidden");
  document.getElementById("parent-body").classList.remove("hidden");
  document.getElementById("parent-detail").classList.add("hidden");
  const list = document.getElementById("parent-list");
  list.classList.remove("hidden");
  parentCurrentId = null;

  const t = data.totals || {};
  const stats = document.getElementById("parent-stats");
  stats.innerHTML = "";
  [[t.conversations, "محادثات"], [t.messages, "رسائل"], [t.leak_flags, "تدخلات فحص الإجابة"]].forEach((s) => {
    const d = document.createElement("div");
    d.className = "parent-stat";
    d.innerHTML = '<span class="num"></span><span class="lbl"></span>';
    d.querySelector(".num").textContent = String(s[0] || 0);
    d.querySelector(".lbl").textContent = s[1];
    stats.appendChild(d);
  });
  document.getElementById("parent-note").textContent = data.logging ? "" :
    "حفظ المحادثات متوقف في الإعدادات، لذلك تظهر فقط محادثات الجلسة الحالية وتُحذف عند إعادة التشغيل.";

  list.innerHTML = "";
  if (!data.conversations.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "لا توجد محادثات بعد";
    list.appendChild(empty);
    return;
  }
  data.conversations.forEach((c) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "parent-row";
    const title = document.createElement("span");
    title.className = "t";
    title.setAttribute("dir", "auto");
    title.textContent = c.title || UNTITLED_LABEL;
    const meta = document.createElement("span");
    meta.className = "m";
    meta.textContent = formatDate(c.updated_at) + " · " + c.message_count + " رسائل";
    row.appendChild(title);
    if (c.leak_flags) {
      const flag = document.createElement("span");
      flag.className = "flag-badge";
      flag.textContent = "تدخل الفحص " + c.leak_flags;
      row.appendChild(flag);
    }
    row.appendChild(meta);
    row.addEventListener("click", () => openParentConversation(c.id));
    list.appendChild(row);
  });
}

async function openParentConversation(id) {
  let data;
  try {
    data = await (await parentFetch("/api/parent/conversations/" + encodeURIComponent(id))).json();
  } catch (e) { return; }
  parentCurrentId = id;
  document.getElementById("parent-list").classList.add("hidden");
  document.getElementById("parent-detail").classList.remove("hidden");
  const thread = document.getElementById("parent-thread");
  thread.innerHTML = "";
  (data.messages || []).forEach((m) => {
    const div = document.createElement("div");
    div.className = "parent-msg " + (m.role === "user" ? "user" : "assistant");
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = m.role === "user" ? "الطالب" : "Mentor";
    div.appendChild(who);
    if (m.image) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = imageUrl(id, m.image);
      div.appendChild(img);
    }
    const body = document.createElement("div");
    body.className = "md-body";
    body.innerHTML = renderMarkdown(m.content || "");
    div.appendChild(body);
    thread.appendChild(div);
    renderMath(body);
  });
}

/* ---------- rendering ---------- */

function hideWelcome() {
  const welcome = document.getElementById("welcome");
  if (welcome) welcome.remove();
}

function showWelcome() {
  const chatBox = document.getElementById("chat-box");
  if (document.getElementById("welcome")) return;
  const welcome = document.createElement("div");
  welcome.className = "welcome";
  welcome.id = "welcome";
  welcome.innerHTML =
    '<div class="welcome-logo" aria-hidden="true">M</div>' +
    "<h1>كيف يمكنني مساعدتك اليوم؟</h1>" +
    "<p>أرسل سؤالك أو صورة الواجب وسأرشدك خطوة بخطوة دون إعطاء الحل مباشرة.</p>";
  chatBox.appendChild(welcome);
}

function renderMessages(convId, messages) {
  const chatBox = document.getElementById("chat-box");
  chatBox.innerHTML = "";
  if (!messages.length) { showWelcome(); return; }
  messages.forEach((msg) => {
    const src = msg.image ? imageUrl(convId, msg.image) : null;
    appendMessage(msg.role === "user" ? "user" : "assistant", msg.content || "", src);
  });
}

function showTyping() {
  const chatBox = document.getElementById("chat-box");
  const msgDiv = document.createElement("div");
  msgDiv.className = "message assistant";
  msgDiv.innerHTML =
    '<div class="avatar" aria-hidden="true">M</div>' +
    '<div class="content"><div class="typing"><span></span><span></span><span></span></div></div>';
  chatBox.appendChild(msgDiv);
  scrollToBottom();
  return msgDiv;
}

function scrollToBottom(onlyIfNear) {
  const scroller = document.getElementById("messages");
  if (!scroller) return;
  if (onlyIfNear && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 160) return;
  scroller.scrollTop = scroller.scrollHeight;
}

function updateUIState(isFullMode, currentRung) {
  const badge = document.getElementById("mode-badge");
  const rungInd = document.getElementById("rung-indicator");
  const unlockBtn = document.getElementById("unlock-btn");
  if (isFullMode) {
    badge.textContent = "الموجه الكامل";
    badge.className = "badge full";
    rungInd.style.display = "none";
    unlockBtn.textContent = "قفل";
    unlockBtn.setAttribute("data-full", "true");
  } else {
    badge.textContent = "وضع المعلم";
    badge.className = "badge mentor";
    rungInd.style.display = "inline";
    rungInd.textContent = "تلميح " + (currentRung || 1) + " من 5";
    unlockBtn.textContent = "إلغاء القفل";
    unlockBtn.setAttribute("data-full", "false");
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Inline markdown: code spans, **bold**, __bold__. HTML is escaped first.
function renderInline(text) {
  const codes = [];
  let s = String(text).replace(/\x60([^\x60]+)\x60/g, (_, code) => {
    codes.push(code);
    return "\u0000" + (codes.length - 1) + "\u0000";
  });
  s = escapeHtml(s);
  s = s.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code dir="ltr">' + escapeHtml(codes[Number(i)]) + "</code>");
  return s;
}

// A "1-" / "1." / "1)" line (optionally wrapped in ** **) becomes a list item
// so the number can sit on the Arabic side instead of being reordered by bidi.
function parseListLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return null;
  let bodyLine = trimmed;
  let forceBold = false;
  const wrapped = trimmed.match(/^(\*\*|__)([\s\S]+)\1$/);
  if (wrapped) { bodyLine = wrapped[2].trim(); forceBold = true; }
  const num = bodyLine.match(/^(\d+)\s*[-.)]\s*([\s\S]+)$/);
  if (num && num[2].trim()) {
    let body = num[2].trim();
    if (forceBold) body = "**" + body + "**";
    return { type: "ol", n: num[1], body: body };
  }
  const markerOnly = trimmed.match(/^\*\*(\d+)\s*[-.)]\*\*\s*([\s\S]+)$/);
  if (markerOnly && markerOnly[2].trim()) return { type: "ol", n: markerOnly[1], body: markerOnly[2].trim() };
  if (!forceBold) {
    const bul = bodyLine.match(/^[-*•]\s+([\s\S]+)$/);
    if (bul && bul[1].trim()) return { type: "ul", body: bul[1].trim() };
  }
  return null;
}

function renderMarkdown(src) {
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let para = [];
  const flushPara = () => {
    para.forEach((l) => out.push('<p dir="auto">' + renderInline(l) + "</p>"));
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === "") {
      flushPara();
      if (out.length && !out[out.length - 1].startsWith('<div class="md-gap"')) out.push('<div class="md-gap"></div>');
      continue;
    }
    const first = parseListLine(lines[i]);
    if (!first) { para.push(lines[i]); continue; }
    flushPara();
    const items = [];
    const type = first.type;
    for (; i < lines.length; i++) {
      if (lines[i].trim() === "") { i--; break; }
      const item = parseListLine(lines[i]);
      if (!item || item.type !== type) { i--; break; }
      const marker = type === "ol"
        ? '<span class="md-num" dir="ltr">' + escapeHtml(item.n) + "-</span>"
        : '<span class="md-num" aria-hidden="true">•</span>';
      items.push('<li class="md-li">' + marker + '<span class="md-item" dir="auto">' + renderInline(item.body) + "</span></li>");
    }
    const tag = type === "ol" ? "ol" : "ul";
    out.push("<" + tag + ' class="md-' + tag + '" dir="rtl">' + items.join("") + "</" + tag + ">");
  }
  flushPara();
  return out.join("");
}

function renderMath(el) {
  if (window.renderMathInElement) {
    window.renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false }
      ],
      throwOnError: false
    });
  }
}

// Returns a handle so streamed text can be appended and re-rendered.
function appendMessage(role, text, imageSrc = null, opts = {}) {
  const chatBox = document.getElementById("chat-box");
  hideWelcome();
  const msgDiv = document.createElement("div");
  msgDiv.className = "message " + role;
  if (role === "assistant") {
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "M";
    msgDiv.appendChild(avatar);
  }
  const contentDiv = document.createElement("div");
  contentDiv.className = "content";
  if (imageSrc) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = imageSrc;
    contentDiv.appendChild(img);
  }
  const textNode = document.createElement("div");
  textNode.className = "md-body" + (opts.streaming ? " streaming" : "");
  contentDiv.appendChild(textNode);
  msgDiv.appendChild(contentDiv);
  chatBox.appendChild(msgDiv);

  const handle = {
    text: text || "",
    render() {
      textNode.innerHTML = renderMarkdown(handle.text);
      scrollToBottom(true);
    },
    finish() {
      textNode.classList.remove("streaming");
      textNode.innerHTML = renderMarkdown(handle.text);
      renderMath(contentDiv);
      scrollToBottom(true);
    }
  };
  if (opts.streaming) {
    handle.render();
  } else {
    handle.finish();
    scrollToBottom();
  }
  return handle;
}
