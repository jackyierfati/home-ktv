const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
const STATE = { done: "已下载", queued: "排队中", downloading: "下载中", failed: "失败", cancelled: "已取消" };
const TABS = ["singers", "songs", "lists", "queue", "system"];

function toast(t) {
  const e = $("#toast");
  e.textContent = t; e.classList.add("show");
  clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove("show"), 2200);
}

async function api(path, body, method) {
  const opts = body === undefined && !method ? {} :
    { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) };
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

const tag = (s) => s ? `<span class="tag ${s}">${STATE[s] || s}</span>` : "";
function songTable(list) {
  if (!list.length) return "<p class=mute>没有歌曲</p>";
  return "<table>" + list.map((x) => `<tr><td>${esc(x.name)}<br><small class=mute>${esc(x.singer)} · ${esc(x.language)}</small></td>
    <td class=r>${tag(x.state)} ${x.state === "done" ? `<button data-redl="${esc(x.number)}" data-name="${esc(x.name)}" title="放起来卡、花屏时用">重新下载</button>`
      : ["queued", "downloading"].includes(x.state) ? "" : `<button data-dl="${esc(x.number)}">下载</button>`}</td></tr>`).join("") + "</table>";
}
const summary = (r) => `新排队 ${r.queued} 首` + (r.existing ? `，已下载跳过 ${r.existing}` : "") + (r.active ? `，已在队列 ${r.active}` : "") + (r.missing ? `，曲库里没有 ${r.missing}` : "");

async function loadSingers() {
  try {
    const d = await api("/singers?q=" + encodeURIComponent($("#sq").value.trim()) + "&limit=60");
    $("#sdetail").innerHTML = "";
    $("#sres").innerHTML = d.map((s) => `<div class=card><h3>${esc(s.name)}</h3><small>${esc(s.region)} · ${esc(s.type)} · 已下 ${s.downloaded}/${s.songs}</small>
      <div class=meter><i style="width:${s.songs ? Math.round(s.downloaded * 100 / s.songs) : 0}%"></i></div>
      <div class=acts><button data-sview="${esc(s.number)}" data-name="${esc(s.name)}">查看歌曲</button><button class=primary data-sall="${esc(s.number)}" data-name="${esc(s.name)}" ${s.downloaded >= s.songs ? "disabled" : ""}>全部下载</button></div></div>`).join("") || "<p class=mute>没有找到歌手</p>";
  } catch (e) { toast(e.message); }
}

async function singerAll(n, name) {
  const lang = $("#lang").value, limit = +$("#limit").value || 0;
  if (!confirm(`把「${name}」${lang ? "的" + lang + "歌曲" : "的全部歌曲"}${limit ? "（最多 " + limit + " 首）" : ""}加入下载队列？`)) return;
  try { toast(summary(await api("/download_singer", { number: n, language: lang, limit }))); loadSingers(); refreshQueue(); }
  catch (e) { toast(e.message); }
}

async function singerView(n, name) {
  try {
    const d = await api("/singer/" + n + "/songs");
    $("#sdetail").innerHTML = `<h2>${esc(name)} · ${d.length} 首</h2>` + songTable(d);
    $("#sdetail").scrollIntoView({ behavior: "smooth" });
  } catch (e) { toast(e.message); }
}

async function searchSongs() {
  const q = $("#q").value.trim(), s = $("#qs").value.trim();
  if (!q && !s) return toast("请输入歌名或歌手");
  try { $("#qres").innerHTML = songTable(await api("/search?q=" + encodeURIComponent(q) + "&singer=" + encodeURIComponent(s))); }
  catch (e) { toast(e.message); }
}

async function loadLists() {
  try {
    const d = await api("/playlists");
    $("#lres").innerHTML = d.map((p) => {
      const type = p.type === "chart" ? 4 : 3;
      return `<div class=card><h3>${esc(p.name)}</h3><small>${p.type === "chart" ? "排行榜" : "歌单"} · ${p.count} 首</small>
        <div class=acts><button data-lview="${esc(p.number)}" data-type="${type}" data-name="${esc(p.name)}">查看歌曲</button><button class=primary data-lall="${esc(p.number)}" data-type="${type}" data-name="${esc(p.name)}">全部下载</button></div></div>`;
    }).join("");
  } catch (e) { toast(e.message); }
}

async function refreshQueue() {
  try {
    const q = await api("/queue");
    $("#qsum").innerHTML = `排队 <b>${q.queued}</b> · 下载中 <b>${q.downloading.length}</b> · 已完成 <b>${q.done}</b>` +
      (q.failed ? ` · 失败 <b>${q.failed}</b>` : "") + (q.paused ? " · <b>已暂停</b>" : "");
    $("#pause").textContent = q.paused ? "继续队列" : "暂停队列";
    $("#pause").dataset.paused = q.paused ? "1" : "";
    if ($("#queue").classList.contains("hidden")) return;
    const jobs = await api("/jobs?limit=60");
    const dl = jobs.filter((j) => j.state === "downloading");
    const qq = jobs.filter((j) => j.state === "queued").reverse();
    const fin = jobs.filter((j) => !["queued", "downloading"].includes(j.state)).reverse();
    $("#qdl").innerHTML = dl.length ? "<table>" + dl.map((j) => `<tr><td>${esc(j.name)}<br><small class=mute>${esc(j.singer)} · ${j.speed_mib || 0} MiB/s ${esc(j.error || "")}</small><div class=meter><i style="width:${j.progress}%"></i></div></td><td class=r>${j.progress}% <button data-cancel="${esc(j.number)}">取消</button></td></tr>`).join("") + "</table>" : "<p class=mute>没有正在下载的歌</p>";
    $("#qq").innerHTML = qq.length ? "<table>" + qq.slice(0, 200).map((j, i) => `<tr><td>${i + 1}. ${esc(j.name)} <small class=mute>${esc(j.singer)}${j.priority ? " · 客人点歌" : ""}</small></td><td class=r><button data-cancel="${esc(j.number)}">移出</button></td></tr>`).join("") + "</table>" + (qq.length > 200 ? `<p class=mute>还有 ${qq.length - 200} 首…</p>` : "") : "<p class=mute>队列是空的</p>";
    $("#qdone").innerHTML = fin.length ? "<table>" + fin.map((j) => `<tr><td>${esc(j.name)} <small class=mute>${esc(j.singer)}</small>${j.error ? `<br><small class=mute>${esc(j.error)}</small>` : ""}</td><td class=r>${tag(j.state)}</td></tr>`).join("") + "</table>" : "<p class=mute>暂无</p>";
  } catch (e) { $("#qsum").textContent = "连接中断"; }
}

function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }
async function loadSystem(remote) {
  try {
    const c = await api("/admin/catalog" + (remote ? "?remote=1" : ""));
    $("#cver").textContent = c.version || "未知";
    $("#csongs").textContent = c.songs;
    if (c.remote) $("#cremote").textContent = c.remote + (c.remote === c.version ? "（已是最新）" : "（有更新）");
    $("#cstate").textContent = c.running ? `更新中 ${c.progress}%` : c.error ? "上次失败：" + c.error : c.finished ? "上次更新完成于 " + when(c.finished) : "空闲";
    $("#cupdate").disabled = c.running;
    if (c.running) setTimeout(() => loadSystem(false), 2000);
    const s = await api("/admin/settings");
    if (document.activeElement !== $("#player")) $("#player").value = s.player_url || "";
    if (document.activeElement !== $("#pmode")) $("#pmode").value = s.player_mode || "tv";
    if (document.activeElement !== $("#rname")) $("#rname").value = s.room_name || "";
    if (![$("#wssid"), $("#wpass"), $("#wsec")].includes(document.activeElement)) {
      $("#wssid").value = s.wifi_ssid || ""; $("#wpass").value = s.wifi_password || ""; $("#wsec").value = s.wifi_security || "WPA";
    }
  } catch (e) { toast(e.message); }
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return; const d = b.dataset;
  if (d.tab) {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
    TABS.forEach((t) => $("#" + t).classList.toggle("hidden", t !== d.tab));
    if (d.tab === "lists" && !$("#lres").children.length) loadLists();
    if (d.tab === "queue") refreshQueue();
    if (d.tab === "system") loadSystem(false);
  }
  if (d.sall) singerAll(d.sall, d.name);
  if (d.sview) singerView(d.sview, d.name);
  if (d.lview) {
    try { const s = await api("/playlist/" + d.lview + "?type=" + d.type); $("#ldetail").innerHTML = `<h2>${esc(d.name)} · ${s.length} 首</h2>` + songTable(s); $("#ldetail").scrollIntoView({ behavior: "smooth" }); }
    catch (x) { toast(x.message); }
  }
  if (d.lall && confirm(`把「${d.name}」全部加入下载队列？`)) {
    try { toast(summary(await api("/download_playlist", { number: d.lall, type: +d.type }))); refreshQueue(); } catch (x) { toast(x.message); }
  }
  if (d.dl) { try { const r = await api("/download_many", { numbers: [d.dl] }); b.outerHTML = tag(r.existing ? "done" : "queued"); refreshQueue(); } catch (x) { toast(x.message); } }
  if (d.redl && confirm(`重新下载《${d.name}》？新文件下完整之前旧的照常留着，下好后自动替换。`)) {
    try { await api("/admin/redownload", { number: d.redl }); b.closest("td").innerHTML = tag("queued"); toast("开始重新下载"); refreshQueue(); }
    catch (x) { toast(x.message); }
  }
  if (d.cancel) { await fetch("/download/" + d.cancel, { method: "DELETE" }); refreshQueue(); }
  if (b.id === "pause") { await api(d.paused ? "/queue/resume" : "/queue/pause", {}); refreshQueue(); }
  if (b.id === "retry") { const r = await api("/queue/retry", {}); toast(`重新排队 ${r.retried} 首`); refreshQueue(); }
  if (b.id === "clear" && confirm("清空批量排队的歌？客人点的歌和正在下载的不受影响。")) { const r = await api("/queue/clear", {}); toast(`已清空 ${r.cleared} 首`); refreshQueue(); }
  if (b.id === "sgo") loadSingers();
  if (b.id === "qgo") searchSongs();
  if (b.id === "ccheck") loadSystem(true);
  if (b.id === "cupdate" && confirm("从 i歌霸重新拉取整个曲库？大约需要几分钟。")) {
    try { await api("/admin/catalog/update", {}); toast("开始更新曲库"); loadSystem(false); } catch (x) { toast(x.message); }
  }
  if (b.id === "rsave") {
    try { await api("/admin/settings", { room_name: $("#rname").value.trim() }, "PUT"); toast("已保存，电视和手机几秒内会更新"); }
    catch (x) { toast(x.message); }
  }
  if (b.id === "wsave") {
    try {
      await api("/admin/settings", { wifi_ssid: $("#wssid").value.trim(), wifi_password: $("#wpass").value, wifi_security: $("#wsec").value }, "PUT");
      toast("已保存，电视主页几秒内会显示");
    } catch (x) { toast(x.message); }
  }
  if (b.id === "psave") {
    try { await api("/admin/settings", { player_url: $("#player").value.trim(), player_mode: $("#pmode").value }, "PUT"); toast("已保存"); } catch (x) { toast(x.message); }
  }
});
$("#sq").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSingers(); });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchSongs(); });
api("/languages").then((d) => { $("#lang").insertAdjacentHTML("beforeend", d.map((l) => `<option>${esc(l.language)}</option>`).join("")); }).catch(() => {});
loadSingers(); refreshQueue(); setInterval(refreshQueue, 2000);
