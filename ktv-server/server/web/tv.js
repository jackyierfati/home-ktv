// 电视主页：长轮询 /api/tv/state，状态一变立刻重画；二维码由电视 App 生成（KtvNative.qr）。
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
const STATUS = { ready: "可以唱", downloading: "下载中", queued: "排队下载", waiting: "等待下载", failed: "下载失败" };
const HINTS = [
  "手机扫右边的码就能点歌 不用装 App",
  "搜拼音首字母也行 比如 qt 晴天 zjl 周杰伦",
  "NAS 上没有的歌 点了它自己去下",
  "点歌时填个昵称 电视上会写是谁点的",
  "不扫码点歌 是想一直看我在主页炫耀写 App 的能力么",
];
const ui = { version: null, uiVersion: null, lastFeed: null, qr: {}, hint: 0, state: null, seen: new Set(), seenFeed: new Set() };

// ---------- 二维码（电视 App 用 ZXing 生成 data URL；在电脑浏览器里打开时没有） ----------
function qr(text, size) {
  const key = size + "|" + text;
  if (!(key in ui.qr)) {
    try { ui.qr[key] = window.KtvNative ? window.KtvNative.qr(text, size) : ""; } catch (e) { ui.qr[key] = ""; }
  }
  return ui.qr[key];
}

function setQr(img, text, size) {
  const src = qr(text, size);
  if (src && img.getAttribute("src") !== src) img.src = src;
  img.classList.toggle("hidden", !src);
}

// ---------- 小部件 ----------
function avatar(song) {
  return song.image
    ? `<img class="avatar" src="${esc(song.image)}" alt="" onerror="this.outerHTML='<div class=avatar>${esc((song.singer || "?").slice(0, 1))}</div>'">`
    : `<div class="avatar">${esc((song.singer || "?").slice(0, 1))}</div>`;
}

function songRow(song, now) {
  const label = song.status === "downloading" ? `下载中 ${song.progress || 0}%` : (STATUS[song.status] || "");
  const pill = now ? `<span class="pill-s ready">正在唱</span>` : (label ? `<span class="pill-s ${song.status}">${label}</span>` : "");
  const bar = song.status === "downloading" ? `<div class="bar"><i style="width:${song.progress || 0}%"></i></div>` : "";
  const by = song.by ? ` · <span class="by">${esc(song.by)} 点的</span>` : "";
  // 只有新进来的歌才有滑入动画，下载进度刷新时不重复播放
  const fresh = ui.seen.has(song.id) ? "" : " new";
  return `<div class="song${now ? " now" : ""}${fresh}" data-id="${esc(song.id)}">${avatar(song)}
    <div class="info"><b>${esc(song.name)}</b><small>${esc(song.singer)}${by}</small>${bar}</div>${pill}</div>`;
}

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  return `${Math.floor(s / 3600)} 小时前`;
}

// 只重画变了的部分，避免列表每 3 秒闪一下
function setHtml(el, html) { if (el._html !== html) { el._html = html; el.innerHTML = html; } }

// ---------- 画面 ----------
function render(s) {
  ui.state = s;
  const playing = !!s.current;
  document.body.classList.toggle("playing", playing);
  document.body.classList.toggle("idle", !playing);
  const guestUrl = location.origin + "/";
  if (s.name && $("#roomName").textContent !== s.name) { $("#roomName").textContent = s.name; document.title = s.name; }
  const st = s.stats || {};
  setHtml($("#stats"), `<b>${(st.catalog || 0).toLocaleString()}</b> 首 · NAS 已缓存 <b>${(st.downloaded || 0).toLocaleString()}</b> 首 · 扫码即点`);

  // 主页最多放 4 行，多的写“还有几首”，不挤到底下的状态栏
  const shown = s.current ? 3 : 4;
  const rows = (s.current ? [songRow(s.current, true)] : []).concat(s.queue.slice(0, shown).map((x) => songRow(x)));
  const more = s.queue.length > shown ? `<p class="more">还有 ${s.queue.length - shown} 首</p>` : "";
  setHtml($("#homeQueue"), rows.join("") + more);
  setHtml($("#queueTitle"), s.queue.length || s.current ? `已点 <small>${s.queue.length + (s.current ? 1 : 0)} 首</small>` : "还没人点歌");
  $("#hint").classList.toggle("hidden", rows.length > 0);
  setHtml($("#panelQueue"), rows.join("") || '<p class="hint">还没有人点歌</p>');

  setQr($("#orderQr"), guestUrl, 300);
  setQr($("#panelOrderQr"), guestUrl, 220);
  $("#orderUrl").textContent = guestUrl;
  const wifi = s.wifi;
  $("#wifiCard").classList.toggle("hidden", !wifi);
  $("#panelWifiCard").classList.toggle("hidden", !wifi);
  if (wifi) {
    setQr($("#wifiQr"), wifi.qr, 300);
    setQr($("#panelWifiQr"), wifi.qr, 220);
    $("#wifiName").textContent = wifi.ssid;
  }

  setHtml($("#feed"), s.feed.slice(-5).reverse().map((e) => `<li${ui.seenFeed.has(e.t) ? "" : ' class="new"'}><span>${esc(e.text)}</span><span>${ago(e.t)}</span></li>`).join("")
    || "<li><span>还没有动静</span><span></span></li>");

  const nas = s.nas || {};
  const parts = [`<span><span class="dot${nas.online ? "" : " off"}">●</span> NAS ${nas.online ? "在线" : "离线"}</span>`];
  if (nas.downloading && nas.downloading.length) {
    const d = nas.downloading[0];
    parts.push(`<span>正在下载《${esc(d.name)}》${d.progress}%</span>`, `<span>${nas.speed} MiB/s</span>`);
  } else if (nas.latest) {
    parts.push(`<span>刚加入《${esc(nas.latest.name)}》${esc(nas.latest.singer)} · ${ago(nas.latest.t)}</span>`);
  }
  parts.push(`<span>${s.connected ? "播放器在线" : "播放器未连接"}</span>`);
  setHtml($("#status"), parts.join(""));

  if (playing) {
    const c = s.current;
    $("#nowText").textContent = `♪ ${c.name} - ${c.singer}${c.by ? `  ·  ${c.by} 点的` : ""}`;
    const n = s.queue[0];
    $("#nextText").textContent = n ? `下一首：${n.name}${n.by ? `（${n.by}）` : ""}${n.status !== "ready" ? ` · ${STATUS[n.status] || ""}` : ""}` : "下一首：还没点";
    const prep = s.queue.filter((x) => x.status !== "ready").slice(0, 3);
    $("#preparing").classList.toggle("hidden", !prep.length);
    setHtml($("#preparing"), prep.map((x) => `${esc(x.name)} ${x.status === "downloading" ? `下载中 ${x.progress}%` : STATUS[x.status] || ""}`).join("<br>"));
  }

  [s.current, ...s.queue].forEach((x) => x && ui.seen.add(x.id));
  s.feed.forEach((e) => ui.seenFeed.add(e.t));

  // 新动态弹出来（第一次加载时不弹旧的）；按时间比较，动态被清空、编号从头开始也不会漏
  const newest = s.feed.length ? s.feed[s.feed.length - 1].t : 0;
  if (ui.lastFeed !== null) s.feed.filter((e) => e.t > ui.lastFeed).forEach((e) => toast(e.text));
  ui.lastFeed = Math.max(ui.lastFeed || 0, newest);
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  $("#toasts").appendChild(el);
  while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 3200);
}

function rotateHint() {
  const el = $("#hint");
  el.classList.add("fade");
  setTimeout(() => { el.textContent = HINTS[ui.hint++ % HINTS.length]; el.classList.remove("fade"); }, 600);
}

// ---------- 遥控器 ← 面板（电视 App 调用） ----------
window.ktv = {
  togglePanel() { $("#panel").classList.toggle("hidden"); return !$("#panel").classList.contains("hidden"); },
  hidePanel() { const open = !$("#panel").classList.contains("hidden"); $("#panel").classList.add("hidden"); return open; },
};

// ---------- 长轮询 ----------
async function loop() {
  for (;;) {
    try {
      const url = "/api/tv/state" + (ui.version === null ? "" : `?since=${ui.version}&wait=3`);
      const res = await fetch(url, { cache: "no-store" });
      const s = await res.json();
      if (ui.uiVersion !== null && s.ui !== ui.uiVersion) { location.reload(); return; }   // 主页改版了
      ui.uiVersion = s.ui;
      ui.version = s.version;
      $("#offline").classList.add("hidden");
      render(s);
    } catch (e) {
      ui.version = null;
      $("#offline").classList.remove("hidden");
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

$("#hint").textContent = HINTS[ui.hint++];
setInterval(rotateHint, 9000);
setInterval(() => { if (ui.state) render(ui.state); }, 30000);   // “x 分钟前”每半分钟更新
loop();
