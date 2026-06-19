/* Nativx Assistant — widget de chat embeddabil (NX-21, Epic E26 W2).
 *
 * Embed de o linie pe site-ul clientului:
 *   <script src="https://<host>/web/widget.js" data-token="pub_..." async></script>
 *
 * Front-end PUR (vanilla JS, fără build/framework), izolat în shadow DOM (zero conflict CSS cu
 * gazda). Consumă gateway-ul NX-20 (#107): GET /web/bootstrap → sesiune (visitor_id semnat HMAC),
 * POST /web/messages → trimite text, GET /web/stream (EventSource SSE) → primește răspunsuri.
 *
 * Tokenul e PUBLIC (identifică tenantul, NU autentifică — gateway-ul rate-limitează + verifică
 * originea, NX-25). ZERO PII în browser în afară de visitor_id-ul opac (localStorage). Disclosure
 * AI permanent în header (art. 50 AI Act). i18n RO/HU/EN pentru chrome (mesajele bot vin localizate
 * din pipeline). Idempotent: nu se montează de două ori.
 *
 * Config din atributele data-* ale tag-ului de embed:
 *   data-token   (obligatoriu) — public token al tenantului
 *   data-api     — base URL gateway (default: originea scriptului); ex. https://api.nativx.tech
 *   data-locale  — ro | hu | en (default ro)
 *   data-title   — titlul din header (default brandul generic)
 *   data-primary — culoarea primară (hex)
 *   data-position- right | left (default right)
 */
(function () {
  "use strict";

  if (window.__nativxWidgetLoaded) return; // idempotent (refresh / dublu embed)
  window.__nativxWidgetLoaded = true;

  var script =
    document.currentScript ||
    (function () {
      var s = document.getElementsByTagName("script");
      return s[s.length - 1];
    })();
  var ds = (script && script.dataset) || {};

  var TOKEN = ds.token || "";
  if (!TOKEN) {
    // Fără token nu avem ce face; nu stricăm pagina gazdei.
    if (window.console) console.warn("[nativx] data-token lipsește — widgetul nu pornește");
    return;
  }

  // Base URL gateway: data-api, altfel originea de la care s-a încărcat scriptul.
  var API = (ds.api || (script && script.src ? new URL(script.src).origin : "")).replace(/\/+$/, "");

  var I18N = {
    ro: {
      title: ds.title || "Asistent",
      disclosure: "Asistent AI · răspunsuri automate",
      placeholder: "Scrie un mesaj…",
      send: "Trimite",
      open: "Chat",
      reconnecting: "Reconectare…",
      rate: "Prea multe mesaje, încearcă din nou în câteva secunde.",
      error: "Conexiune indisponibilă. Reîncerc…",
      unavailable: "Chat indisponibil momentan.",
    },
    hu: {
      title: ds.title || "Asszisztens",
      disclosure: "AI asszisztens · automatikus válaszok",
      placeholder: "Írj egy üzenetet…",
      send: "Küldés",
      open: "Csevegés",
      reconnecting: "Újracsatlakozás…",
      rate: "Túl sok üzenet, próbáld újra pár másodperc múlva.",
      error: "A kapcsolat nem elérhető. Újrapróbálom…",
      unavailable: "A csevegés jelenleg nem elérhető.",
    },
    en: {
      title: ds.title || "Assistant",
      disclosure: "AI assistant · automated replies",
      placeholder: "Type a message…",
      send: "Send",
      open: "Chat",
      reconnecting: "Reconnecting…",
      rate: "Too many messages, try again in a few seconds.",
      error: "Connection unavailable. Retrying…",
      unavailable: "Chat unavailable right now.",
    },
  };
  var lang = ds.locale && I18N[ds.locale] ? ds.locale : "ro";
  var L = I18N[lang];

  var THEME = {
    primary: ds.primary || "#1f6feb",
    position: ds.position === "left" ? "left" : "right",
  };

  // --- Shadow DOM: izolare totală față de pagina gazdă -----------------------
  var host = document.createElement("div");
  host.id = "nativx-assistant-root";
  var root = host.attachShadow({ mode: "open" });
  document.body.appendChild(host);

  var CSS =
    ":host{all:initial}" +
    "*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}" +
    ".launcher{position:fixed;bottom:20px;" +
    THEME.position +
    ":20px;z-index:2147483000;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;" +
    "background:var(--nx-primary);color:#fff;font-size:24px;box-shadow:0 4px 14px rgba(0,0,0,.25)}" +
    ".panel{position:fixed;bottom:88px;" +
    THEME.position +
    ":20px;z-index:2147483000;width:340px;max-width:calc(100vw - 32px);height:480px;" +
    "max-height:calc(100vh - 120px);background:#fff;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.3);" +
    "display:none;flex-direction:column;overflow:hidden}" +
    ".panel.open{display:flex}" +
    ".hdr{background:var(--nx-primary);color:#fff;padding:12px 14px}" +
    ".hdr .t{font-weight:600;font-size:15px}" +
    ".hdr .d{font-size:11px;opacity:.85;margin-top:2px}" +
    ".msgs{flex:1;overflow-y:auto;padding:12px;background:#f6f7f9;display:flex;flex-direction:column;gap:8px}" +
    ".b{max-width:80%;padding:8px 11px;border-radius:12px;font-size:14px;line-height:1.35;white-space:pre-wrap;word-wrap:break-word}" +
    ".b.bot{background:#fff;border:1px solid #e6e8eb;align-self:flex-start;border-bottom-left-radius:4px}" +
    ".b.user{background:var(--nx-primary);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}" +
    ".status{font-size:11px;color:#8a8f98;text-align:center;padding:2px}" +
    ".in{display:flex;border-top:1px solid #e6e8eb;padding:8px;gap:6px;background:#fff}" +
    ".in input{flex:1;border:1px solid #d0d3d8;border-radius:18px;padding:9px 12px;font-size:14px;outline:none}" +
    ".in button{border:0;background:var(--nx-primary);color:#fff;border-radius:18px;padding:0 14px;cursor:pointer;font-size:14px}" +
    ".in button:disabled{opacity:.5;cursor:default}";

  var HTML =
    '<button class="launcher" part="launcher" aria-label="' +
    esc(L.open) +
    '">💬</button>' +
    '<section class="panel" role="dialog" aria-label="' +
    esc(L.title) +
    '">' +
    '<div class="hdr"><div class="t">' +
    esc(L.title) +
    '</div><div class="d">' +
    esc(L.disclosure) +
    "</div></div>" +
    '<div class="msgs"></div>' +
    '<div class="status"></div>' +
    '<form class="in"><input type="text" placeholder="' +
    esc(L.placeholder) +
    '" autocomplete="off" maxlength="2000"/>' +
    '<button type="submit">' +
    esc(L.send) +
    "</button></form>" +
    "</section>";

  root.innerHTML = "<style>" + CSS + "</style>" + HTML;
  host.style.setProperty("--nx-primary", THEME.primary);
  // CSS var trebuie pe :host → o setăm pe shadow host element (moștenit în shadow).
  host.style.cssText += ";--nx-primary:" + THEME.primary;

  var $launcher = root.querySelector(".launcher");
  var $panel = root.querySelector(".panel");
  var $msgs = root.querySelector(".msgs");
  var $status = root.querySelector(".status");
  var $form = root.querySelector(".in");
  var $input = root.querySelector(".in input");
  var $send = root.querySelector(".in button");

  // --- Stare sesiune (visitor opac în localStorage, namespace-uit pe token) --
  var SKEY = "nx_session_" + TOKEN.slice(0, 10);
  var visitorId = null;
  var sig = null;
  var es = null;
  var started = false;

  function enc(v) {
    return encodeURIComponent(v);
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function setStatus(t) {
    $status.textContent = t || "";
  }
  function addBubble(who, text) {
    var d = document.createElement("div");
    d.className = "b " + (who === "user" ? "user" : "bot");
    d.textContent = text;
    $msgs.appendChild(d);
    $msgs.scrollTop = $msgs.scrollHeight;
  }

  function loadSession() {
    try {
      var o = JSON.parse(localStorage.getItem(SKEY) || "null");
      if (o && o.visitor_id && o.sig) {
        visitorId = o.visitor_id;
        sig = o.sig;
      }
    } catch (e) {
      /* localStorage indisponibil (private mode) → sesiune doar în memorie */
    }
  }
  function saveSession() {
    try {
      localStorage.setItem(SKEY, JSON.stringify({ visitor_id: visitorId, sig: sig }));
    } catch (e) {
      /* ignore */
    }
  }

  async function bootstrap() {
    if (visitorId && sig) return true;
    var r = await fetch(API + "/web/bootstrap?token=" + enc(TOKEN), { credentials: "omit" });
    if (!r.ok) return false;
    var j = await r.json();
    visitorId = j.visitor_id;
    sig = j.sig;
    saveSession();
    return true;
  }

  function openStream() {
    if (es) return;
    var url =
      API + "/web/stream?token=" + enc(TOKEN) + "&visitor_id=" + enc(visitorId) + "&sig=" + enc(sig);
    es = new EventSource(url);
    es.onopen = function () {
      setStatus("");
    };
    es.onmessage = function (e) {
      try {
        var d = JSON.parse(e.data);
        if (d && d.type === "text" && d.text) addBubble("bot", d.text);
      } catch (err) {
        /* frame ne-JSON (heartbeat) → ignorat */
      }
    };
    es.onerror = function () {
      // EventSource reconectează singur (+ Last-Event-ID gestionat de browser/gateway).
      setStatus(L.reconnecting);
    };
  }

  async function ensureStarted() {
    if (started) return true;
    try {
      if (!(await bootstrap())) {
        setStatus(L.unavailable);
        return false;
      }
      openStream();
      started = true;
      return true;
    } catch (e) {
      setStatus(L.unavailable);
      return false;
    }
  }

  async function sendMessage(text) {
    addBubble("user", text);
    $send.disabled = true;
    try {
      var r = await fetch(API + "/web/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "omit",
        body: JSON.stringify({ token: TOKEN, visitor_id: visitorId, sig: sig, text: text }),
      });
      if (r.status === 429) setStatus(L.rate);
      else if (!r.ok) setStatus(L.error);
      else setStatus("");
    } catch (e) {
      setStatus(L.error);
    } finally {
      $send.disabled = false;
    }
  }

  // --- Evenimente UI ---------------------------------------------------------
  $launcher.addEventListener("click", async function () {
    var opening = !$panel.classList.contains("open");
    $panel.classList.toggle("open");
    if (opening) {
      await ensureStarted();
      $input.focus();
    }
  });

  $form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var text = ($input.value || "").trim();
    if (!text) return;
    if (!(await ensureStarted())) return;
    $input.value = "";
    sendMessage(text);
  });

  loadSession();
})();
