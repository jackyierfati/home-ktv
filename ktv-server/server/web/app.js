const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const enc = encodeURIComponent;
const ui = { mode: "song", back: null, current: null, room: null, ordered: new Set(), dragging: false, filter: { region: "", type: "" }, view: "all" };
const STATUS = {
  ready: "可以唱", downloading: "下载中", queued: "排队下载", waiting: "等待下载", failed: "下载失败"
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method, cache: "no-store", headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch (error) {
    throw new Error("连不上点歌服务，请检查 Wi-Fi");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "操作失败");
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(el.timer);
  el.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

// ---------- 找歌 ----------
function songRow(song) {
  const ordered = ui.ordered.has(song.number);
  const hint = song.ready ? "" : (song.state === "downloading" || song.state === "queued") ? "准备中" : "需下载";
  return `<div class="song" data-number="${esc(song.number)}">
    <div class="info"><b>${esc(song.name)}${hint ? `<span class="hint">${hint}</span>` : ""}</b>
    <small>${esc(song.singer)}${song.language ? " · " + esc(song.language) : ""}</small></div>
    <button class="order primary"${ordered ? " disabled" : ""}>${ordered ? "已点" : "点歌"}</button></div>`;
}

function show(html, back = null, render = null) {
  ui.back = back;
  if (render) ui.current = render;
  $("#content").innerHTML = (back ? '<button class="back">‹ 返回</button>' : "") + html;
  window.scrollTo({ top: 0 });
  hookAvatars($("#content"));
}

/** 头像加载失败时换成名字首字。 */
function hookAvatars(root) {
  $$("img.avatar:not([data-hooked])", root).forEach((img) => {
    img.dataset.hooked = "1";
    img.addEventListener("error", () => {
      const div = document.createElement("div");
      div.className = "avatar";
      div.textContent = img.dataset.initial || "?";
      img.replaceWith(div);
    }, { once: true });
  });
}

/** 分页歌曲列表：load(page) 返回 {songs, hasMore}。 */
function mountList(load) {
  const list = $("#content .list"), more = $("#content .more");
  let page = 1;
  async function next() {
    more.disabled = true;
    try {
      const data = await load(page);
      if (page === 1 && !data.songs.length) list.innerHTML = '<div class="empty">没有找到歌曲</div>';
      list.insertAdjacentHTML("beforeend", data.songs.map(songRow).join(""));
      more.classList.toggle("hidden", !data.hasMore);
      page += 1;
      return data;
    } catch (error) { toast(error.message); }
    finally { more.disabled = false; }
  }
  more.onclick = next;
  return next();
}

const songList = '<div class="list"></div><button class="more hidden">加载更多</button>';
const avatar = (s) => s.image
  ? `<img class="avatar" src="${esc(s.image)}" alt="" loading="lazy" data-initial="${esc(s.name.slice(0, 1))}">`
  : `<div class="avatar">${esc(s.name.slice(0, 1))}</div>`;

function setCat(cat) {
  $$("#cats button").forEach((b) => b.classList.toggle("on", b.dataset.cat === cat));
}

function showHot() {
  setCat("hot");
  show(`<div class="title"><h2>热门歌曲</h2></div>${songList}`, null, showHot);
  mountList((page) => api(`/api/hot?page=${page}`));
}

function chips(key, values) {
  return `<div class="chips" data-filter="${key}">` + values.map(([value, label]) =>
    `<button data-value="${value}" class="${ui.filter[key] === value ? "on" : ""}">${label}</button>`).join("") + "</div>";
}

function showSingers() {
  setCat("singers");
  show(chips("region", [["", "全部"], ["大陆", "内地"], ["港台", "港台"], ["欧美", "欧美"], ["日韩", "日韩"]]) +
    chips("type", [["", "全部"], ["男", "男歌手"], ["女", "女歌手"], ["组合", "组合"]]) +
    '<div id="singers" class="grid"></div><button class="more hidden">加载更多</button>', null, showSingers);
  const grid = $("#singers"), more = $("#content .more");
  let page = 1;
  async function next() {
    more.disabled = true;
    try {
      const d = await api(`/api/singers?region=${enc(ui.filter.region)}&type=${enc(ui.filter.type)}&page=${page}`);
      grid.insertAdjacentHTML("beforeend", d.singers.map(singerTile).join(""));
      hookAvatars(grid);
      more.classList.toggle("hidden", !d.hasMore);
      page += 1;
    } catch (error) { toast(error.message); }
    finally { more.disabled = false; }
  }
  more.onclick = next;
  next();
}

const singerTile = (s) => `<button class="tile" data-singer="${esc(JSON.stringify(s))}">${avatar(s)}<b>${esc(s.name)}</b><small>${esc(s.region)}</small></button>`;

function showSinger(singer, back) {
  show(`<div class="title">${avatar(singer)}<div><h2>${esc(singer.name)}</h2><small class="mute" id="singerTotal"></small></div></div>${songList}`, back);
  mountList((page) => api(`/api/singers/${singer.number}/songs?page=${page}`)).then((d) => {
    if (d) $("#singerTotal").textContent = `${d.total} 首`;
  });
}

async function showCharts(kind) {
  setCat(kind);
  show('<div id="tiles" class="grid"></div>', null, () => showCharts(kind));
  try {
    const d = await api("/api/charts");
    const lists = kind === "charts" ? d.charts : d.playlists;
    $("#tiles").innerHTML = lists.map((x) => `<button class="tile wide" data-list="${esc(JSON.stringify(x))}"><b>${esc(x.name)}</b><small>${x.count} 首</small></button>`).join("");
  } catch (error) { toast(error.message); }
}

function showList(list, back) {
  show(`<div class="title"><h2>${esc(list.name)}</h2></div>${songList}`, back);
  mountList((page) => api(`/api/charts/${list.number}/songs?type=${list.type}&page=${page}`));
}

async function showLanguages() {
  setCat("languages");
  show('<div id="tiles" class="grid"></div>', null, showLanguages);
  try {
    const d = await api("/api/languages");
    $("#tiles").innerHTML = d.languages.map((l) => `<button class="tile wide" data-language="${esc(l.name)}"><b>${esc(l.name)}</b><small>${l.count} 首</small></button>`).join("");
  } catch (error) { toast(error.message); }
}

function showLanguage(name, back) {
  show(`<div class="title"><h2>${esc(name)}</h2></div>${songList}`, back);
  mountList((page) => api(`/api/languages/${enc(name)}/songs?page=${page}`));
}

const singerChip = (s) => `<button class="singer-chip" data-singer="${esc(JSON.stringify(s))}">${avatar(s)}<span>${esc(s.name)} 的歌</span><small>›</small></button>`;

async function showSearch(q) {
  setCat("");
  const singerMode = ui.mode === "singer";
  show(`<div class="title"><h2>搜索“${esc(q)}”</h2></div>` +
    (singerMode ? '<div id="hits" class="grid"></div><h3>歌曲</h3>' : '<div id="hits" class="singer-chips"></div>') + songList,
    null, () => showSearch(q));
  mountList((page) => api(`/api/songs?q=${enc(q)}&mode=${ui.mode}&page=${page}`));
  // 按歌名搜时，名字或拼音完全对上的歌手放在最上面（zjl → 周杰伦 的歌）
  const hits = $("#hits");
  try {
    const d = await api(`/api/singers?q=${enc(q)}${singerMode ? "" : "&exact=1"}`);
    if (!hits.isConnected) return;                       // 已经翻到别的页面了
    hits.innerHTML = singerMode ? d.singers.slice(0, 12).map(singerTile).join("") : d.singers.map(singerChip).join("");
    hookAvatars(hits);
    if (!d.singers.length) hits.remove();
  } catch (error) { if (hits.isConnected) toast(error.message); }
}

const cats = { hot: showHot, singers: showSingers, charts: () => showCharts("charts"), playlists: () => showCharts("playlists"), languages: showLanguages };

// ---------- 昵称：电视上显示是谁点的，存在这台手机上 ----------
function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key) || "";
    localStorage.setItem(key, value);
  } catch (error) { /* 无痕模式之类存不了，就不记 */ }
  return value;
}

function showName() {
  const name = storage("ktv_name");
  $("#me").textContent = name ? `我是 ${name}` : "设置昵称";
}

function askName() {
  $("#nameInput").value = storage("ktv_name");
  $("#nameDialog").classList.remove("hidden");
  setTimeout(() => $("#nameInput").focus(), 50);
}

function saveName(name) {
  storage("ktv_name", name.trim().slice(0, 16));
  storage("ktv_name_asked", "1");
  $("#nameDialog").classList.add("hidden");
  showName();
}

// ---------- 我点的：点歌成功时服务端给的 id 记在这台手机上，同名昵称点的也算 ----------
function mineIds() {
  try { return new Set(JSON.parse(storage("ktv_mine") || "[]")); } catch (error) { return new Set(); }
}

function rememberMine(id) {
  storage("ktv_mine", JSON.stringify([...mineIds(), id].slice(-30)));
}

const isMine = (item, ids, name) => ids.has(item.id) || (!!name && item.by === name);

/** 轮到 items[index] 还要多久：前面几首（正在放的也算），时间按歌长估，正在放的那首按剩下的算。 */
function turn(items, index) {
  const item = items[index];
  if (item.current) return "正在唱";
  if (index === 0) return item.status === "ready" ? "马上开始" : "下载好就开唱";
  const wait = items.slice(0, index).reduce((sum, x) =>
    sum + (x.current ? Math.max(0, (x.duration || 0) - (x.position || 0)) : (x.duration || 0)), 0);
  const head = index === 1 && items[0].current ? "下一首就是你" : `前面还有 ${index} 首`;
  return head + (wait ? ` · 大约 ${Math.max(1, Math.round(wait / 60))} 分钟` : "");
}

function orderedText(res) {
  if (!res.id) return "点歌成功";
  if (res.now) return "点好了 现在就开唱";
  const ready = res.status === "ready";
  const head = res.ahead === 0 ? (ready ? "马上开始" : "下载好就开唱") : res.ahead === 1 ? "下一首就是你" : `前面还有 ${res.ahead} 首`;
  return "点好了 " + head + (ready || res.ahead === 0 ? "" : " · NAS 正在下载");
}

async function order(number, button) {
  button.disabled = true;
  try {
    const res = await api("/api/room/queue", { method: "POST", body: { number, by: storage("ktv_name") } });
    if (res.id) rememberMine(res.id);
    ui.ordered.add(number);
    button.textContent = "已点";
    toast(orderedText(res));
    refreshRoom();
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

// ---------- 已点和遥控 ----------
function statusPill(item) {
  const label = item.status === "downloading" ? `下载中 ${item.progress || 0}%` : STATUS[item.status] || "";
  return label ? `<span class="pill ${item.status}">${label}</span>` : "";
}

/** n：正在放的显示 ♪，后面的从 1 排起；mine：是我点的；when：轮到我还要多久（“我点的”里显示）。 */
function queueItem(item, n, mine = false, when = "") {
  const bar = item.status === "downloading" ? `<div class="bar"><i style="width:${item.progress || 0}%"></i></div>` : "";
  // 还在下载的歌电视那边还没收到，只能等下载完；不能置顶或删除
  const buttons = item.current || item.pending ? "" :
    (item.status === "failed" ? `<button data-retry="${esc(item.number)}">重试</button>` : "") +
    `<button data-top="${esc(item.id)}">置顶</button><button data-remove="${esc(item.id)}">删除</button>`;
  const note = item.pending ? '<small class="mute">下载好后自动开始</small>' : "";
  const by = mine ? ' · <span class="me-tag">我点的</span>' : item.by ? ` · ${esc(item.by)} 点的` : "";
  return `<div class="item${item.current ? " current" : ""}${mine ? " mine" : ""}">
    <span class="n">${item.current ? "♪" : n}</span>
    <div class="info"><b>${esc(item.name)}</b><small>${esc(item.singer)}${by} ${statusPill(item)}</small>${when ? `<small class="turn">${esc(when)}</small>` : ""}${note}${bar}</div>${buttons}</div>`;
}

function renderRoom(room) {
  ui.room = room;
  if (room.name && $("#roomName").textContent !== room.name) {
    $("#roomName").textContent = room.name;
    document.title = room.name + " 点歌";
  }
  const p = room.player;
  $("#tv").textContent = p.connected ? "电视已连接" : (p.error || "电视未连接");
  $("#tv").className = "tv " + (p.connected ? "on" : "off");
  $("#nowName").textContent = p.current?.name || "暂无歌曲";
  $("#nowSinger").textContent = p.current ? p.current.singer : (p.connected ? "点一首歌开始唱吧" : "电视未连接");
  $("#play").textContent = p.playing ? "暂停" : "继续";
  $("#play").dataset.action = p.playing ? "pause" : "resume";
  $("#original").classList.toggle("on", p.vocalMode === "original");
  $("#accompany").classList.toggle("on", p.vocalMode !== "original");
  // 电视播放器模式下麦克风走混音器，没有麦克风音量
  $("#micVolume").closest("label").classList.toggle("hidden", p.micVolume == null && p.connected);
  if (!ui.dragging) ["music", "mic"].forEach((k) => {
    const v = p[k + "Volume"] ?? 70;
    $(`#${k}Volume`).value = v;
    $(`#${k}Value`).textContent = v;
  });
  // 麦动要等歌下完才放进已点；正在下载的歌接在已点后面显示
  const inQueue = new Set(room.queue.map((i) => i.number));
  const pending = room.downloads.filter((d) => !inQueue.has(d.number))
    .map((d) => ({ ...d, pending: true, status: d.state === "downloading" ? "downloading" : "queued" }));
  const queue = [...room.queue, ...pending];
  $("#queueCount").textContent = queue.length ? `${queue.length} 首` : "";
  $("#tabCount").textContent = queue.length;
  $("#tabCount").classList.toggle("hidden", !queue.length);
  const ids = mineIds(), name = storage("ktv_name"), offset = queue[0]?.current ? 0 : 1;
  const mine = queue.map((item, i) => ({ item, i })).filter(({ item }) => isMine(item, ids, name));
  // 只有 Home KTV 播放器模式有条目 id，麦动模式不分“我点的”
  const split = room.version != null, view = split ? ui.view : "all";
  $("#views").classList.toggle("hidden", !split);
  $$("#views button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $("#mineCount").textContent = mine.length || "";
  $("#queue").innerHTML = view === "mine"
    ? mine.map(({ item, i }) => queueItem(item, i + offset, true, turn(queue, i))).join("") ||
      '<div class="empty">你还没点歌<br><small>点了的歌会在这里显示还要等多久</small></div>'
    : queue.map((item, i) => queueItem(item, i + offset, isMine(item, ids, name))).join("") || '<div class="empty">还没有点歌</div>';
  // 点歌页顶上：我点的下一首还要等多久，点一下看“我点的”
  const next = mine[0];
  $("#mineBar").classList.toggle("hidden", !next);
  if (next) $("#mineBar").innerHTML = `<span>我点的《${esc(next.item.name)}》</span><b>${esc(turn(queue, next.i))}</b>` +
    `<small>${mine.length > 1 ? `共 ${mine.length} 首 ` : ""}›</small>`;
  ui.ordered = new Set(queue.map((i) => i.number).filter(Boolean));
  $$("#content .song").forEach((row) => {
    const button = $(".order", row);
    const ordered = ui.ordered.has(row.dataset.number);
    if (ordered) { button.disabled = true; button.textContent = "已点"; }
    else if (button.textContent === "已点") { button.disabled = false; button.textContent = "点歌"; }
  });
}

async function refreshRoom() {
  try { renderRoom(await api("/api/room")); }
  catch (error) { $("#tv").textContent = error.message; $("#tv").className = "tv off"; }
}

/** 长轮询已点：电视那边一变（有人点歌、切歌、下载好了）马上更新；麦动模式没有版本号，照旧 2 秒拉一次。 */
async function roomLoop() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let version = null;
  for (;;) {
    if (document.hidden) { await sleep(1000); continue; }
    try {
      const room = await api("/api/room" + (version != null ? `?since=${version}&wait=3` : ""));
      version = room.version ?? null;
      renderRoom(room);
      if (version == null) await sleep(2000);
    } catch (error) {
      version = null;
      $("#tv").textContent = error.message; $("#tv").className = "tv off";
      await sleep(2000);
    }
  }
}

async function control(path, method, body) {
  try { await api(path, { method, body }); refreshRoom(); }
  catch (error) { toast(error.message); }
}

// ---------- 事件 ----------
document.addEventListener("click", (event) => {
  const b = event.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  if (d.tab) {
    document.body.dataset.tab = d.tab;
    $$(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    window.scrollTo({ top: 0 });
    return;
  }
  if (b.classList.contains("back") && ui.back) return ui.back();
  if (d.view || d.mineBar) {
    ui.view = d.view || "mine";
    storage("ktv_view", ui.view);
    if (d.mineBar) $('.tabs button[data-tab="room"]').click();   // 手机上切到“已点”标签
    if (ui.room) renderRoom(ui.room);
    return;
  }
  if (d.mode) {
    ui.mode = d.mode;
    $$("[data-mode]").forEach((x) => x.classList.toggle("on", x === b));
    $("#q").placeholder = ui.mode === "singer" ? "歌手名或拼音首字母" : "歌名或拼音首字母";
    return;
  }
  if (d.cat) return cats[d.cat]();
  if (d.singer) return showSinger(JSON.parse(d.singer), ui.current);
  if (d.list) return showList(JSON.parse(d.list), ui.current);
  if (d.language) return showLanguage(d.language, ui.current);
  const group = b.closest(".chips");
  if (group) { ui.filter[group.dataset.filter] = d.value; return showSingers(); }
  if (b.classList.contains("order")) return order(b.closest(".song").dataset.number, b);
  if (d.action) return control("/api/room/player", "POST", { action: d.action });
  if (d.vocal) return control("/api/room/audio", "PATCH", { vocalMode: d.vocal });
  if (d.top) return control("/api/room/queue/" + enc(d.top), "PATCH", {});
  if (d.remove) return control("/api/room/queue/" + enc(d.remove), "DELETE");
  if (d.retry) return control("/api/room/retry", "POST", { number: d.retry });
});

$("#searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return $("#q").focus();
  $("#q").blur();
  showSearch(q);
});

["music", "mic"].forEach((k) => {
  const slider = $(`#${k}Volume`);
  slider.addEventListener("input", () => { ui.dragging = true; $(`#${k}Value`).textContent = slider.value; });
  slider.addEventListener("change", () => { ui.dragging = false; control("/api/room/audio", "PATCH", { [k + "Volume"]: Number(slider.value) }); });
});

$("#me").addEventListener("click", askName);
$("#nameOk").addEventListener("click", () => saveName($("#nameInput").value));
$("#nameSkip").addEventListener("click", () => saveName(storage("ktv_name")));
$("#nameInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveName($("#nameInput").value); });
showName();
if (!storage("ktv_name_asked")) askName();
ui.view = storage("ktv_view") === "mine" ? "mine" : "all";

roomLoop();
showHot();
