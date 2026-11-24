"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtNum = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n || 0));
const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(t * 1000).toLocaleDateString();
};
const media = (p) => (p ? "/media?p=" + encodeURIComponent(p) : "");
const PF = { youtube: "YouTube", twitch: "Twitch", kick: "Kick" };
const STATUS = { live: "Live", starting: "Starting", connecting: "Connecting", reconnecting: "Reconnecting", offline: "Offline" };

let S = null;
const ui = { status: "review", tags: new Set(), settingsDirty: 0, view: {}, stylesFilled: false, openIdeas: new Set() };
try { const t = localStorage.getItem("cw-theme"); if (t) document.documentElement.dataset.theme = t; } catch {}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}

/* ---------- top bar ---------- */
function renderTop() {
  const live = S.streams.filter((s) => s.status === "live").length;
  $("#runningLabel").textContent = S.running ? "On" : "Off";
  $("#pausedBar").hidden = !!S.running;
  const review = S.moments.filter((m) => m.status === "ready").length;
  const q = S.queue;
  const busy = q.busy ? (q.busy.kind === "vertical" ? "Making 9:16…" : "Cutting clip…") : "Idle";
  $("#topStats").innerHTML = `
    <span class="stat"><span class="dot ${live ? "on" : ""}"></span><b>${live}</b>/${S.settings.max_streams} live</span>
    <span class="stat"><b>${review}</b> to review</span>
    <span class="stat">${busy}${q.queued ? ` · ${q.queued} queued` : ""}</span>
    ${diskStat()}
    ${(() => { const n = S.moments.filter(askPosted).length; return n ? `<button class="stat ask-pill" id="askBtn" title="Clips you kept or opened in Finder: did you post them?">${n} posted?</button>` : ""; })()}`;
  if (Date.now() - ui.settingsDirty > 3000) {
    $("#autopilot").checked = !!S.settings.autopilot;
    if (document.activeElement !== $("#maxStreams")) $("#maxStreams").value = S.settings.max_streams;
    $("#running").checked = !!S.running;
  }
}

/* ---------- side: live now / top clips ---------- */
function renderLive() {
  const d = S.discovered;
  const watching = new Set(S.streams.map((s) => s.url));
  $("#liveCount").textContent = d.at ? `${d.streams.length} live · ${ago(d.at)}` : "Searching…";
  $("#notes").innerHTML = (d.notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join("");
  const html = d.streams.map((s, i) => `
    <li class="live-item">
      <img src="${esc(s.thumb)}" alt="" loading="lazy">
      <div>
        <div class="who"><span class="pf ${s.platform}" title="${PF[s.platform]}"></span><span>${esc(s.display)}</span></div>
        <div class="t" title="${esc(s.title)}">${esc(s.title)}</div>
        <div class="row">
          <span class="viewers">${s.viewers ? fmtNum(s.viewers) + " watching" : PF[s.platform]}</span>
          ${watching.has(s.url) ? `<span class="muted" style="font-size:12px">Watching</span>`
            : `<button class="btn sm" data-watch="${i}">Watch</button>`}
        </div>
      </div>
    </li>`).join("");
  if (html !== renderLive.last) { $("#liveList").innerHTML = html; renderLive.last = html; }
}

function renderTopClips() {
  const t = S.top_clips;
  $("#topNote").innerHTML = t.note ? `<div class="note">${esc(t.note)}</div>` : "";
  const html = t.clips.map((c, i) => `
    <li class="live-item">
      <img src="${esc(c.thumb)}" alt="" loading="lazy">
      <div>
        <div class="who"><span class="pf twitch"></span><span>${esc(c.channel)}</span></div>
        <div class="t" title="${esc(c.title)}">${esc(c.title)}</div>
        <div class="row">
          <span class="viewers">${fmtNum(c.views)} views · ${Math.round(c.duration)}s</span>
          <button class="btn sm" data-grab="${i}">Grab</button>
        </div>
      </div>
    </li>`).join("");
  if (html !== renderTopClips.last) { $("#topList").innerHTML = html; renderTopClips.last = html; }
}

/* ---------- scout ---------- */
function pct(v) { return v == null ? "–" : Math.round(v * 100) + "%"; }
function renderScout() {
  const sc = S.scout || { ranked: [], notes: [] };
  const when = sc.last_refresh ? ago(sc.last_refresh) : "…";
  $("#scoutStatus").textContent = `${sc.ranked.length} streamers · ${sc.status} · scanned ${when}`;
  $("#scoutNotes").innerHTML = (sc.notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join("");
  const ignored = new Set(sc.ignored || []);
  const html = sc.ranked.map((r, i) => {
    const st = r.stats || {};
    const name = r.display || r.channel;
    const pill = r.watching ? `<span class="pill rec">${r.auto ? "Autopilot" : "Recording"}</span>`
      : r.warming ? `<span class="pill warm">Listening…</span>` : "";
    const m = [];
    if (r.platform !== "kick") {
      m.push(`<span>laughs <b>${pct(r.laughs)}</b></span>`);
      m.push(`<span>chat <b>${st.chat_per_min ?? 0}</b>/min</span>`);
      m.push(`<span>spikes <b>${r.spikes == null ? "–" : r.spikes}</b>/h</span>`);
      if (r.engagement != null) m.push(`<span>per 1K viewers <b>${r.engagement}</b></span>`);
    } else m.push(`<span>audio only (no Kick chat)</span>`);
    if (r.viewers) m.push(`<span><b>${fmtNum(r.viewers)}</b> viewers</span>`);
    if (r.clips != null) m.push(`<span><b>${r.clips}</b> clips · ${fmtNum(r.clip_views || 0)} views</span>`);
    const h = r.hist || {};
    if (h.kept || h.rejected) m.push(`<span>you kept <b>${h.kept}</b>/${h.kept + h.rejected}</span>`);
    return `
    <li class="scout-row ${r.watching ? "watching" : ""} ${ignored.has(r.id) ? "ignored" : ""}">
      <div>
        <div class="who"><span class="pf ${r.platform}"></span><span class="nm">${esc(name)}</span>
          ${r.country ? `<span class="flag">${esc(r.country)}</span>` : ""}${pill}</div>
        <div class="cps" title="Clip potential score"><span class="bar"><i style="width:${r.cps}%"></i></span><b>${r.cps}</b></div>
        <div class="metrics">${m.join("")}</div>
      </div>
      <div class="acts">
        ${r.watching ? `<button class="btn sm ghost" data-unwatch="${esc(r.id)}">Stop</button>`
          : `<button class="btn sm" data-scoutwatch="${i}">Record</button>`}
        <button class="btn sm ghost" data-ignore="${esc(r.id)}" title="Autopilot skips ignored streamers">${ignored.has(r.id) ? "Unignore" : "Ignore"}</button>
      </div>
    </li>`;
  }).join("");
  if (html !== renderScout.last) { $("#scoutList").innerHTML = html || `<li class="empty">Scanning live GTA 6 streams…</li>`; renderScout.last = html; }
  const off = (sc.offtopic || []).map((c) => esc(c.display || c.channel)).join(", ");
  $("#offtopic").innerHTML = off ? `<p class="offt">Live but not playing GTA right now: ${off}</p>` : "";
}

/* ---------- watching ---------- */
function streamCard(s) {
  const L = s.last || {};
  const err = s.error && s.status !== "live" ? `<div class="s-err">${esc(s.error)}</div>` : "";
  const chat = s.platform === "kick" ? "audio only" : s.has_chat ? `${fmtNum(s.msgs)} chat msgs` : "waiting for chat";
  return `
    <article class="stream" data-id="${esc(s.id)}">
      <div class="s-top">
        <span class="pf ${s.platform}" title="${PF[s.platform]}"></span>
        <h3>${esc(s.channel)}</h3>
        ${s.auto ? `<span class="badge" title="Picked by autopilot">auto</span>` : ""}
        ${fcBadge(s.fc_key)}
        <span class="status ${s.status}">${STATUS[s.status] || s.status}</span>
      </div>
      <div class="s-title" title="${esc(s.title)}">${esc(s.title || s.url)}</div>
      <canvas class="spark" height="64"></canvas>
      <div class="meters">
        <div class="meter"><small>Score</small><b class="v-score">${(L.score ?? 0).toFixed(1)}</b></div>
        <div class="meter"><small>Chat</small><b class="v-rate">${(L.rate ?? 0).toFixed(1)}/s</b></div>
        <div class="meter"><small>Laughs</small><b class="v-laugh">${Math.round((L.laugh ?? 0) * 100)}%</b></div>
        <div class="meter"><small>Audio</small><b class="v-audio">${(L.audio_z ?? 0).toFixed(1)}σ</b></div>
      </div>
      ${err}
      <div class="s-foot">
        <span class="muted v-foot">${chat} · ${Math.round(s.buffered)}s buffered · ${s.clips_hour ?? 0} clips last hour</span>
        <button class="btn sm primary" data-clip="${esc(s.id)}" ${s.status === "live" ? "" : "disabled"}>Clip now</button>
        <button class="btn sm ghost" data-fcsid="${esc(s.id)}" ${s.buffered > 2 ? "" : "disabled"} title="Draw where this streamer's webcam is">Facecam</button>
        <button class="btn sm ghost" data-unwatch="${esc(s.id)}">Stop</button>
      </div>
    </article>`;
}

function drawSpark(canvas, hist, threshold) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const c = canvas.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const max = Math.max(threshold * 1.4, ...hist, 1);
  const y = (v) => h - 6 - (Math.max(0, v) / max) * (h - 12);
  c.strokeStyle = css.getPropertyValue("--muted");
  c.setLineDash([4, 4]); c.lineWidth = 1;
  c.beginPath(); c.moveTo(0, y(threshold)); c.lineTo(w, y(threshold)); c.stroke();
  c.setLineDash([]);
  if (hist.length < 2) return;
  const step = w / 89;
  const x0 = w - (hist.length - 1) * step;
  const accent = css.getPropertyValue("--accent").trim();
  c.beginPath();
  hist.forEach((v, i) => (i ? c.lineTo(x0 + i * step, y(v)) : c.moveTo(x0, y(v))));
  c.strokeStyle = accent; c.lineWidth = 2; c.lineJoin = "round"; c.stroke();
  c.lineTo(x0 + (hist.length - 1) * step, h); c.lineTo(x0, h); c.closePath();
  c.fillStyle = accent + "22"; c.fill();
}

function renderStreams() {
  const box = $("#streams");
  $("#watchCount").textContent = S.streams.length ? `${S.streams.length}/${S.settings.max_streams}` : "";
  if (!S.running) {
    box.innerHTML = `<div class="empty">ClipWatch is off. Streams on your list start recording when you turn it on.</div>`;
    box.dataset.ids = "";
    return;
  }
  if (!S.streams.length) {
    box.innerHTML = `<div class="empty">Nothing watched yet. Pick a stream on the left or paste a link.<br>Up to ${S.settings.max_streams} at once on this Mac.</div>`;
    return;
  }
  const ids = S.streams.map((s) => s.id).join("|");
  if (box.dataset.ids !== ids) { box.innerHTML = S.streams.map(streamCard).join(""); box.dataset.ids = ids; }
  for (const s of S.streams) {
    const el = box.querySelector(`[data-id="${CSS.escape(s.id)}"]`);
    if (!el) continue;
    const sig = [s.status, s.error, s.title, s.channel, S.facecams?.[s.fc_key]?.mode, s.buffered > 2].join("¦");
    if (el.dataset.sig !== sig) {
      const fresh = document.createRange().createContextualFragment(streamCard(s)).firstElementChild;
      el.replaceWith(fresh); fresh.dataset.sig = sig;
      drawSpark(fresh.querySelector("canvas"), s.history, s.rule?.threshold ?? S.settings.threshold);
      continue;
    }
    const L = s.last || {};
    el.querySelector(".v-score").textContent = (L.score ?? 0).toFixed(1);
    el.querySelector(".v-rate").textContent = (L.rate ?? 0).toFixed(1) + "/s";
    el.querySelector(".v-laugh").textContent = Math.round((L.laugh ?? 0) * 100) + "%";
    el.querySelector(".v-audio").textContent = (L.audio_z ?? 0).toFixed(1) + "σ";
    const chat = s.platform === "kick" ? "audio only" : s.has_chat ? `${fmtNum(s.msgs)} chat msgs` : "waiting for chat";
    el.querySelector(".v-foot").textContent = `${chat} · ${Math.round(s.buffered)}s buffered · ${s.clips_hour ?? 0} clips last hour`;
    el.classList.toggle("firing", (L.score ?? 0) >= (s.rule?.threshold ?? S.settings.threshold));
    drawSpark(el.querySelector("canvas"), s.history, s.rule?.threshold ?? S.settings.threshold);
  }
}

/* ---------- moments ---------- */
function momentCard(m) {
  const waiting = m.status === "clipping";
  const failed = m.status === "failed";
  const gone = !!m.cleaned;
  const ready = !waiting && !failed && m.clip && !gone;
  const lenKind = m.edit_info?.kind || m.kind;
  const isLong = lenKind === "long" || lenKind === "best";
  let vid;
  if (waiting) vid = `<div class="wait"><div class="spinner"></div>Cutting clip…</div>`;
  else if (gone) vid = `${m.thumb ? `<img src="${esc(media(m.thumb))}" alt="">` : ""}<div class="wait gone">Video files deleted${m.status === "posted" ? " after posting" : ""}</div>`;
  else if (failed || !m.clip) vid = `<div class="wait">No clip</div>`;
  else {
    const tall = m.vertical && (ui.view[m.id] ?? "9:16") === "9:16";
    const srcp = tall ? m.vertical : m.clip;
    vid = `<video controls preload="metadata" playsinline ${tall ? "" : `poster="${esc(media(m.thumb))}"`} src="${esc(media(srcp))}"></video>`;
  }
  const chat = (m.chat || []).length
    ? `<details class="chat"><summary>Chat at that moment (${m.chat.length})</summary><ul>${m.chat.map((c) => `<li><b>${esc(c.user)}</b> ${esc(c.text)}</li>`).join("")}</ul></details>` : "";
  const es = m.edit_state;
  const info = m.edit_info || {};
  const styleOpts = (S.styles || []).map((x) => `<option value="${x.key}" ${x.key === (m.style || S.settings.edit_style) ? "selected" : ""}>${esc(x.label)}</option>`).join("");
  const editState = es === "queued" ? `<div class="edit-state"><span class="spinner"></span>9:16 edit queued…</div>`
    : es === "editing" ? `<div class="edit-state"><span class="spinner"></span>Making the 9:16 edit…</div>`
    : es === "failed" ? `<div class="edit-state">9:16 edit failed</div>`
    : es === "skipped" ? `<div class="edit-state">Edit queue was full, click Make 9:16</div>`
    : es === "lowdisk" ? `<div class="edit-state">Disk almost full: free space, then click Make 9:16</div>`
    : m.vertical ? `<div class="edit-meta">${info.kind === "best" ? "best moment · " : info.kind === "long" ? "long highlight · " : ""}${esc((S.styles || []).find((x) => x.key === info.style)?.label || "9:16")} · ${camText(info)} · ${info.words || 0} words captioned${info.cuts ? ` · ${info.cuts} jump cuts (−${info.cut_s}s)` : ""}${info.replay ? " · slow-mo replay" : ""} · ${Math.round(info.length || 0)}s</div>` : "";
  const curLen = info.kind || m.kind || "short";
  const plan = m.plan || {};
  const thinking = m.ai_state === "thinking";
  const aiErr = m.ai_state === "failed" && (m.error || "").startsWith("Claude:");
  const busyEdit = thinking || es === "queued" || es === "editing";
  const ideas = [...(info.ideas || []).map((x, i) => ({ ...x, src: "h", i })), ...(m.ai_ideas || []).map((x, i) => ({ ...x, src: "a", i }))];
  const aiBlock = ready ? `
        <div class="m-ai">
          ${Object.keys(plan).length ? `<div class="plan-line"><span>Your edit${plan.summary ? `: ${esc(plan.summary)}` : ""}</span><button class="linkbtn" data-act="resetplan" ${busyEdit ? "disabled" : ""}>Reset to automatic</button></div>` : ""}
          ${ideas.length ? `<details class="ideas" ${ui.openIdeas.has(m.id) ? "open" : ""}><summary>Edit ideas (${ideas.length})</summary><ul>${ideas.map((x) => `
            <li><div><b>${esc(x.title)}</b>${x.why ? `<small>${esc(x.why)}</small>` : ""}${x.src === "a" ? `<small class="by">from Claude</small>` : ""}</div>
              <button class="btn sm" data-idea="${x.src}:${x.i}" ${busyEdit ? "disabled" : ""}>Apply</button></li>`).join("")}</ul></details>` : ""}
          <form class="ai-form" data-mid="${m.id}">
            <input name="q" type="text" maxlength="400" autocomplete="off" placeholder="Tell Claude how to edit it: start at the crash, slow-mo replay…" aria-label="How Claude should edit this clip" ${thinking ? "disabled" : ""}>
            <button class="btn sm primary" type="submit" ${thinking ? "disabled" : ""}>Edit</button>
          </form>
          <div class="ai-row">
            ${thinking ? `<span class="edit-state"><span class="spinner"></span>Claude is working on it…</span>`
              : aiErr ? `<span class="m-err">${esc(m.error.replace(/^Claude: /, "Claude couldn't do that: "))}</span>`
              : m.ai_request && Object.keys(plan).length ? `<span class="asked">You asked: “${esc(m.ai_request)}”</span>` : "<span></span>"}
            <button class="linkbtn" data-act="aiideas" ${thinking ? "disabled" : ""}>More ideas from Claude</button>
          </div>
        </div>` : "";
  const editor = ready ? `
        <div class="m-edit">
          <input type="text" maxlength="60" placeholder="Hook title (blank = automatic)" value="${esc(info.hook_locked ? m.hook || "" : "")}" aria-label="Hook title">
          <select class="sty" aria-label="Edit style">${styleOpts}</select>
          <div class="row2">${editState || `<span class="edit-meta">${m.hook ? "Hook: " + esc(m.hook) : ""}</span>`}
            <select class="len" aria-label="Highlight length"><option value="short" ${curLen === "short" ? "selected" : ""}>Short</option><option value="long" ${curLen === "long" ? "selected" : ""}>Long 30–45s</option><option value="best" ${curLen === "best" ? "selected" : ""}>Best 60–90s</option></select>
            <button class="btn sm primary" data-act="vertical" ${es === "queued" || es === "editing" ? "disabled" : ""}>${m.vertical ? "Re-render" : "Make 9:16"}</button></div>
        </div>` : "";
  return `
    <article class="moment ${m.status}" data-mid="${m.id}">
      <div class="vid ${m.vertical && (ui.view[m.id] ?? "9:16") === "9:16" && ready ? "tall" : ""}">${vid}
        ${m.vertical && ready ? `<span class="viewtoggle"><button data-view="16:9" class="${(ui.view[m.id] ?? "9:16") === "16:9" ? "on" : ""}">16:9</button><button data-view="9:16" class="${(ui.view[m.id] ?? "9:16") === "9:16" ? "on" : ""}">9:16</button></span>` : ""}
        <span class="tag ${esc(m.tag)}">${esc(m.tag === "topclip" ? "top clip" : m.tag)}${lenKind === "best" ? " · best" : isLong ? " · long" : ""}</span>
        ${m.score ? `<span class="score">${Number(m.score).toFixed(1)}</span>` : ""}
      </div>
      <div class="m-body">
        <div class="m-who"><span class="pf ${m.platform}"></span><span class="n">${esc(m.channel)}</span><time>${ago(m.created)}</time></div>
        ${m.delete_in != null ? `<div class="ask del"><span>Posted ✓ · deleting its video files in ${Math.ceil(m.delete_in)} s</span><button class="btn sm" data-act="unpost">Undo</button></div>`
          : askPosted(m) ? `<div class="ask"><span>Posted this on TikTok?</span><button class="btn sm posted" data-act="posted" data-del="1">Yes · delete files</button><button class="btn sm ghost" data-act="notyet">Not yet</button></div>` : ""}
        <div class="reasons">${(m.reasons || []).map((r) => `<span>${esc(r)}</span>`).join("")}</div>
        ${chat}
        ${editor}
        ${aiBlock}
        ${m.error && !aiErr ? `<div class="m-err">${esc(m.error)}</div>` : ""}
      </div>
      <div class="m-actions">
        <button class="btn sm keep" data-act="keep" ${ready ? "" : "disabled"}>${m.status === "kept" ? "Kept ✓" : "Keep"}</button>
        <button class="btn sm posted ${m.status === "posted" ? "on" : ""}" data-act="${m.status === "posted" ? "unpost" : "posted"}" data-del="1" ${ready ? "" : "disabled"} title="${m.status === "posted" ? "Mark as not posted" : "You posted it on TikTok: its video files are deleted 10 s later (Undo on the card)"}">${m.status === "posted" ? "Posted ✓" : "Posted"}</button>
        <button class="btn sm reject" data-act="reject" ${waiting || gone ? "disabled" : ""}>Reject</button>
        <button class="btn sm ghost icon" data-act="facecam" ${ready ? "" : "disabled"} title="Facecam: draw where the webcam is, once per streamer" aria-label="Facecam"><svg viewBox="0 0 24 24"><path d="M15 8v8H5V8h10m1-2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4V7a1 1 0 0 0-1-1z"/></svg></button>
        <button class="btn sm ghost icon" data-act="reveal" ${ready ? "" : "disabled"} title="Show in Finder" aria-label="Show in Finder"><svg viewBox="0 0 24 24"><path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg></button>
      </div>
    </article>`;
}

function renderMoments() {
  const box = $("#moments");
  const list = S.moments.filter((m) => {
    if (ui.status === "review" && !["ready", "clipping", "failed", "detected"].includes(m.status)) return false;
    if (ui.status === "kept" && m.status !== "kept") return false;
    if (ui.status === "posted" && m.status !== "posted") return false;
    if (ui.status === "rejected" && m.status !== "rejected") return false;
    if (ui.tags.has("long") && !["long", "best"].includes(m.edit_info?.kind || m.kind)) return false;
    const tags = [...ui.tags].filter((t) => t !== "long");
    return !tags.length || tags.includes(m.tag);
  });
  if (!list.length) {
    box.innerHTML = `<div class="empty">${ui.status === "review" ? "No moments to review yet. They show up here as soon as a stream spikes." : "Nothing here."}</div>`;
    box.dataset.ids = "";
    return;
  }
  const ids = list.map((m) => m.id).join(",");
  if (box.dataset.ids !== ids) {
    // keep cards that didn't change so a playing video isn't interrupted
    const old = new Map([...box.querySelectorAll(".moment")].map((el) => [el.dataset.mid, el]));
    const frag = document.createDocumentFragment();
    for (const m of list) {
      const sig = sigOf(m);
      let el = old.get(String(m.id));
      if (!el || (el.dataset.sig !== sig && !el.contains(document.activeElement))) { el = document.createRange().createContextualFragment(momentCard(m)).firstElementChild; el.dataset.sig = sig; }
      frag.appendChild(el);
    }
    box.replaceChildren(frag);
    box.dataset.ids = ids;
    return;
  }
  for (const m of list) {
    const el = box.querySelector(`[data-mid="${m.id}"]`);
    const sig = sigOf(m);
    if (el && el.dataset.sig !== sig && !el.contains(document.activeElement)) {
      const fresh = document.createRange().createContextualFragment(momentCard(m)).firstElementChild;
      fresh.dataset.sig = sig; el.replaceWith(fresh);
    }
  }
}
const sigOf = (m) => [m.ai_state, m.ai_request, JSON.stringify(m.plan || {}), (m.ai_ideas || []).length, (m.edit_info?.ideas || []).map((x) => x.title).join("/"), m.status, m.clip, m.vertical, m.error, m.thumb, m.edit_state, m.hook, m.style, ui.view[m.id], m.edit_info?.cam, m.edit_info?.kind, m.kind, m.cleaned, m.delete_in != null ? Math.ceil(m.delete_in) : "", askPosted(m)].join("¦");

/* ---------- loop ---------- */
async function refresh() {
  try {
    S = await api("/api/state");
    renderTop(); renderScout(); renderLive(); renderTopClips(); renderStreams(); renderRules(); renderMoments();
  } catch (e) {
    $("#topStats").innerHTML = `<span class="stat"><span class="dot"></span>Server not reachable</span>`;
  }
}
setInterval(refresh, 2000);
// entry animation: stays at least 1.6 s, leaves as soon as the dashboard has its data (max 5 s)
const splashAt = Date.now();
function hideSplash() {
  const sp = $("#splash");
  if (!sp || sp.classList.contains("out")) return;
  setTimeout(() => {
    sp.classList.add("out");
    setTimeout(() => sp.remove(), 700);
  }, Math.max(0, 1600 - (Date.now() - splashAt)));
}
refresh().finally(hideSplash);
setTimeout(hideSplash, 5000);

/* ---------- events ---------- */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.watch !== undefined) {
      const s = S.discovered.streams[+b.dataset.watch];
      b.disabled = true;
      await api("/api/watch", { url: s.url, title: s.title, display: s.display });
      toast(`Watching ${s.display}`);
    } else if (b.dataset.grab !== undefined) {
      const c = S.top_clips.clips[+b.dataset.grab];
      b.disabled = true;
      await api("/api/grab", c);
      toast("Downloading clip…");
    } else if (b.dataset.clip) {
      await api("/api/clip", { id: b.dataset.clip });
      toast("Clipping the last 35 seconds…");
    } else if (b.dataset.unwatch) {
      await api("/api/unwatch", { id: b.dataset.unwatch });
      toast("Stopped");
    } else if (b.dataset.scoutwatch !== undefined) {
      const r = S.scout.ranked[+b.dataset.scoutwatch];
      b.disabled = true;
      await api("/api/watch", { url: r.url, title: r.title, display: r.display || r.channel });
      toast(`Recording ${r.display || r.channel}`);
    } else if (b.dataset.ignore) {
      await api("/api/scout/ignore", { id: b.dataset.ignore });
    } else if (b.dataset.fcsid) {
      return openFacecam({ sid: b.dataset.fcsid });
    } else if (b.dataset.idea) {
      const id = +b.closest(".moment").dataset.mid;
      const m = S.moments.find((x) => x.id === id);
      const [src, i] = b.dataset.idea.split(":");
      const idea = (src === "a" ? m.ai_ideas : m.edit_info?.ideas || [])[+i];
      if (!idea) return;
      b.disabled = true;
      await api("/api/plan", { id, merge: idea.plan });
      toast(`Applying “${idea.title}”: re-rendering…`);
    } else if (b.dataset.act === "resetplan") {
      await api("/api/plan", { id: +b.closest(".moment").dataset.mid, reset: true });
      toast("Back to the automatic edit: re-rendering…");
    } else if (b.dataset.act === "aiideas") {
      b.disabled = true;
      await api("/api/ai-ideas", { id: +b.closest(".moment").dataset.mid });
      toast("Claude is watching the clip's transcript for ideas…");
    } else if (b.dataset.act === "facecam") {
      return openFacecam({ mid: +b.closest(".moment").dataset.mid });
    } else if (b.dataset.view) {
      const card = b.closest(".moment");
      ui.view[+card.dataset.mid] = b.dataset.view;
    } else if (b.dataset.act) {
      const card = b.closest(".moment");
      const id = +card.dataset.mid;
      const body = { id, action: b.dataset.act };
      if (b.dataset.act === "posted" && b.dataset.del) body.delete = true;
      if (b.dataset.act === "vertical") {
        body.style = card.querySelector(".m-edit select.sty")?.value;
        body.length = card.querySelector(".m-edit select.len")?.value;
        body.hook = card.querySelector(".m-edit input")?.value ?? "";
      }
      await api("/api/review", body);
      if (b.dataset.act === "vertical") toast(body.length === "long" ? "Making a 30–45 s highlight…" : "Making the 9:16 edit…");
      if (b.dataset.act === "posted") toast("Posted ✓ · its video files are deleted in 10 s (Undo on the card)");
      if (b.dataset.act === "unpost") toast("Undone: the clip and its files stay");
    } else if (b.dataset.side) {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === b));
      $("#side-scout").hidden = b.dataset.side !== "scout";
      $("#side-live").hidden = b.dataset.side !== "live";
      $("#side-top").hidden = b.dataset.side !== "top";
      if (b.dataset.side === "top" && !S.top_clips.at) $("#refreshTop").click();
    } else if (b.closest("#statusFilter")) {
      ui.status = b.dataset.v;
      b.parentElement.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    } else if (b.closest("#tagFilter")) {
      ui.tags.has(b.dataset.v) ? ui.tags.delete(b.dataset.v) : ui.tags.add(b.dataset.v);
      b.classList.toggle("on");
    } else if (b.id === "refreshScout") {
      await api("/api/scout/refresh", {});
      toast("Rescanning live GTA 6 streams…");
    } else if (b.id === "refreshLive") {
      b.disabled = true; b.textContent = "Searching…";
      await api("/api/discover", {}).finally(() => { b.disabled = false; b.textContent = "Refresh"; });
    } else if (b.id === "refreshTop") {
      b.disabled = true; b.textContent = "Loading…";
      await api("/api/topclips", {}).finally(() => { b.disabled = false; b.textContent = "Load"; });
    } else if (b.id === "cleanBtn") {
      return openCleanup();
    } else if (b.id === "askBtn") {
      return openAsk();
    } else if (b.dataset.askyes || b.dataset.askno) {
      const id = +(b.dataset.askyes || b.dataset.askno);
      await api("/api/review", b.dataset.askyes ? { id, action: "posted", delete: true } : { id, action: "notyet" });
      b.closest("li")?.remove();
      if (!$("#askList").children.length) $("#askDialog").close();
    } else if (b.id === "askAll") {
      const ids = [...$("#askList").querySelectorAll("[data-askyes]")].map((x) => +x.dataset.askyes);
      for (const id of ids) await api("/api/review", { id, action: "posted", delete: true });
      $("#askDialog").close();
      toast(`${ids.length} marked as posted · their video files are deleted in 10 s (Undo on each card)`);
    } else if (b.id === "openFolder") {
      await api("/api/open-folder", {});
    } else if (b.id === "themeBtn") {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("cw-theme", next); } catch {}
    } else return;
    refresh();
  } catch (err) {
    toast(err.message);
    b.disabled = false;
  }
});

$("#pasteForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#pasteUrl").value.trim();
  if (!url) return;
  try {
    await api("/api/watch", { url });
    $("#pasteUrl").value = "";
    toast("Watching");
    refresh();
  } catch (err) { toast(err.message); }
});


$("#autopilot").addEventListener("change", (e) => { ui.settingsDirty = Date.now(); api("/api/settings", { autopilot: e.target.checked }); toast(e.target.checked ? "Autopilot on: recording the top-ranked streams" : "Autopilot off"); });
$("#maxStreams").addEventListener("change", (e) => {
  const v = Math.max(1, Math.min(10, parseInt(e.target.value, 10) || 3));
  ui.settingsDirty = Date.now();
  api("/api/settings", { max_streams: v }).then(() => toast(`Up to ${v} streams at once${S.settings.autopilot ? " — autopilot fills the slots" : ""}`));
});


/* ---------- facecam box: draw the webcam once per streamer ---------- */
const fc = { mid: null, sid: null, key: null, name: "", style: null, t: null, box: null, auto: null, url: null, drag: null };
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

function fcBadge(key) {
  const f = S?.facecams?.[key];
  if (!f) return "";
  return f.mode === "none" ? `<span class="badge" title="You set: no webcam">no cam</span>`
    : `<span class="badge cam" title="Using the facecam box you drew">cam ✓</span>`;
}
function camText(info) {
  if (info.cam === "drawn") return info.box_face === false ? "your facecam box (no face seen in it)" : "your facecam box";
  if (info.cam === "none") return "no facecam (your setting)";
  return info.facecam ? "facecam found" : "no facecam";
}
function fcStyle() {
  const all = S.styles || [];
  const st = all.find((x) => x.key === fc.style);
  return st && (st.layout === "stack" || st.layout === "bubble") ? st : all.find((x) => x.key === "streamer") || { key: "streamer", label: "Streamer stack", layout: "stack", face_h: 700 };
}

function openFacecam({ mid = null, sid = null }) {
  const m = mid ? S.moments.find((x) => x.id === mid) : null;
  const s = sid ? S.streams.find((x) => x.id === sid) : null;
  const src = m || s;
  if (!src) return;
  Object.assign(fc, { mid, sid, key: src.fc_key, name: src.channel, t: null, drag: null });
  fc.style = m?.style || S.settings.edit_style;
  const saved = S.facecams?.[fc.key];
  fc.box = saved?.mode === "box" ? { x: saved.x, y: saved.y, w: saved.w, h: saved.h } : null;
  // the crop the last automatic edit of this streamer used, as a dashed reference
  const prev = (m && m.edit_info?.cam === "auto" && m.edit_info.cam_crop ? m : null)
    || S.moments.find((x) => x.fc_key === fc.key && x.edit_info?.cam === "auto" && x.edit_info?.cam_crop);
  const cc = prev?.edit_info.cam_crop;
  fc.auto = cc ? { x: cc[0], y: cc[1], w: cc[2], h: cc[3] } : null;
  $("#fcName").textContent = fc.name;
  $("#fcState").textContent = saved ? (saved.mode === "none" ? "Now: no facecam (your setting)" : "Now: your box (shown)") : "Now: automatic";
  const waiting = S.moments.filter((x) => x.fc_key === fc.key && x.status === "ready" && x.clip && x.id !== mid).length;
  $("#fcRedoWrap").hidden = !waiting;
  $("#fcRedoTxt").textContent = mid ? `Also redo ${fc.name}'s other ${waiting} clip${waiting === 1 ? "" : "s"} waiting for review (max 10)`
    : `Redo ${fc.name}'s ${waiting} clip${waiting === 1 ? "" : "s"} waiting for review (max 10)`;
  $("#fcOther").textContent = mid ? "Other frame" : "Newest frame";
  $("#fcDialog").showModal();
  fcLoad();
}

async function fcLoad() {
  const load = $("#fcLoading");
  load.hidden = false;
  load.innerHTML = `<span class="spinner"></span><span>Grabbing a frame…</span>`;
  const q = fc.mid ? `mid=${fc.mid}${fc.t != null ? `&t=${fc.t}` : ""}` : `sid=${encodeURIComponent(fc.sid)}`;
  try {
    const r = await fetch(`/api/frame?${q}&_=${Date.now()}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Couldn't get a frame");
    const t = r.headers.get("X-Frame-Time");
    if (t) fc.t = parseFloat(t);
    const url = URL.createObjectURL(await r.blob());
    const img = $("#fcImg");
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("Bad frame")); img.src = url; });
    if (fc.url) URL.revokeObjectURL(fc.url);
    fc.url = url;
    $("#fcStage").style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
    load.hidden = true;
    fcDraw();
  } catch (e) {
    load.innerHTML = `<span>${esc(e.message)}</span>`;
  }
}

function fcDraw() {
  const put = (el, b) => {
    el.hidden = !b;
    if (b) Object.assign(el.style, { left: b.x * 100 + "%", top: b.y * 100 + "%", width: b.w * 100 + "%", height: b.h * 100 + "%" });
  };
  put($("#fcBox"), fc.box);
  put($("#fcAuto"), fc.box ? null : fc.auto);
  $("#fcSave").disabled = !fc.box;
  fcPreview();
}

function fcPreview() {
  const cv = $("#fcPreview"), img = $("#fcImg"), c = cv.getContext("2d");
  const W = cv.width, H = cv.height, k = W / 1080;
  c.fillStyle = "#000"; c.fillRect(0, 0, W, H);
  if (!img.naturalWidth || !$("#fcLoading").hidden) return;
  const iw = img.naturalWidth, ih = img.naturalHeight;
  const st = fcStyle();
  const cam = fc.box || fc.auto;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  // biggest crop of this aspect inside box b, placed like the editor does (face sits a bit above the middle)
  const crop = (aspect, b) => {
    const bx = b.x * iw, by = b.y * ih, bw = b.w * iw, bh = b.h * ih;
    let w = bw, h = bw / aspect;
    if (h > bh) { h = bh; w = bh * aspect; }
    return [bx + (bw - w) / 2, clamp(by + bh * 0.42 - h * 0.42, by, by + bh - h), w, h];
  };
  const full = { x: 0, y: 0, w: 1, h: 1 };
  $("#fcPrevCap").textContent = `9:16 preview · ${st.label}${cam ? "" : " (no facecam: whole frame)"}`;
  if (!cam) {
    const vh = W * ih / iw;
    c.drawImage(img, 0, (H - vh) / 2, W, vh);
    return;
  }
  if (st.layout === "bubble") {
    c.drawImage(img, ...crop(1080 / 1920, full), 0, 0, W, H);
    const d = 380 * k, px = 44 * k, py = 1110 * k;
    c.save(); c.beginPath(); c.arc(px + d / 2, py + d / 2, d / 2, 0, Math.PI * 2); c.clip();
    c.drawImage(img, ...crop(1, cam), px, py, d, d); c.restore();
    c.strokeStyle = "#fff"; c.lineWidth = 3; c.beginPath(); c.arc(px + d / 2, py + d / 2, d / 2, 0, Math.PI * 2); c.stroke();
    return;
  }
  let fh = st.face_h || 700;
  if (fc.box) fh = clamp(1080 * (fc.box.h * ih) / (fc.box.w * iw), fh * 0.8, Math.min(fh * 1.25, 1100));
  const gh = 1920 - fh;
  c.drawImage(img, ...crop(1080 / fh, cam), 0, 0, W, fh * k);
  c.drawImage(img, ...crop(1080 / gh, full), 0, fh * k, W, gh * k);
  c.fillStyle = accent; c.fillRect(0, fh * k - 1, W, 2);
}

function fcPt(e) {
  const r = $("#fcStage").getBoundingClientRect();
  return { x: clamp((e.clientX - r.left) / r.width, 0, 1), y: clamp((e.clientY - r.top) / r.height, 0, 1) };
}
$("#fcStage").addEventListener("pointerdown", (e) => {
  if (!$("#fcLoading").hidden || e.button > 0) return;
  const p = fcPt(e);
  const h = e.target.dataset?.h;
  if (h && fc.box) fc.drag = { mode: "resize", h, start: p, orig: { ...fc.box } };
  else if (fc.box && e.target.closest("#fcBox")) fc.drag = { mode: "move", start: p, orig: { ...fc.box } };
  else fc.drag = { mode: "draw", start: p, prev: fc.box };
  $("#fcStage").setPointerCapture(e.pointerId);
  e.preventDefault();
});
function fcMove(e) {
  const d = fc.drag;
  if (!d) return;
  const p = fcPt(e), o = d.orig;
  if (d.mode === "draw") {
    fc.box = { x: Math.min(d.start.x, p.x), y: Math.min(d.start.y, p.y), w: Math.abs(p.x - d.start.x), h: Math.abs(p.y - d.start.y) };
  } else if (d.mode === "move") {
    fc.box = { ...o, x: clamp(o.x + p.x - d.start.x, 0, 1 - o.w), y: clamp(o.y + p.y - d.start.y, 0, 1 - o.h) };
  } else {
    let x0 = o.x, y0 = o.y, x1 = o.x + o.w, y1 = o.y + o.h;
    if (d.h.includes("w")) x0 = p.x;
    if (d.h.includes("e")) x1 = p.x;
    if (d.h.includes("n")) y0 = p.y;
    if (d.h.includes("s")) y1 = p.y;
    fc.box = { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
  }
  fcDraw();
}
$("#fcStage").addEventListener("pointermove", fcMove);
const fcUp = (e) => {
  const d = fc.drag;
  if (!d) return;
  if (e.type === "pointerup") fcMove(e);  // the release point counts too (fast drags skip the last move)
  fc.drag = null;
  if (fc.box && (fc.box.w < 0.03 || fc.box.h < 0.03)) fc.box = d.mode === "draw" ? d.prev : d.orig;  // a click, not a drag
  fcDraw();
};
$("#fcStage").addEventListener("pointerup", fcUp);
$("#fcStage").addEventListener("pointercancel", fcUp);
$("#fcBox").addEventListener("keydown", (e) => {
  const k = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
  if (!k || !fc.box) return;
  e.preventDefault();
  const step = 0.005, b = { ...fc.box };
  if (e.shiftKey) { b.w = clamp(b.w + k[0] * step, 0.03, 1 - b.x); b.h = clamp(b.h + k[1] * step, 0.03, 1 - b.y); }
  else { b.x = clamp(b.x + k[0] * step, 0, 1 - b.w); b.y = clamp(b.y + k[1] * step, 0, 1 - b.h); }
  fc.box = b;
  fcDraw();
});

async function fcSave(mode) {
  const body = { mode, redo_all: !$("#fcRedoWrap").hidden && $("#fcRedo").checked };
  if (fc.mid) body.mid = fc.mid; else body.sid = fc.sid;
  if (mode === "box") body.box = [fc.box.x, fc.box.y, fc.box.w, fc.box.h].map((v) => +v.toFixed(4));
  try {
    const r = await api("/api/facecam", body);
    $("#fcDialog").close();
    const what = mode === "box" ? `Facecam saved for ${fc.name}` : mode === "none" ? `${fc.name}: no facecam` : `${fc.name}: automatic again`;
    toast(what + (r.redone ? ` · redoing ${r.redone} clip${r.redone === 1 ? "" : "s"}` : " · used for the next clips"));
    refresh();
  } catch (err) { toast(err.message); }
}
$("#fcSave").addEventListener("click", () => fc.box && fcSave("box"));
$("#fcNone").addEventListener("click", () => fcSave("none"));
$("#fcAutoBtn").addEventListener("click", () => fcSave("auto"));
$("#fcOther").addEventListener("click", () => { if (fc.mid) fc.t = (fc.t ?? 0) + 6; fcLoad(); });
$("#fcDialog").addEventListener("close", () => {
  fc.drag = null;
  if (fc.url) { URL.revokeObjectURL(fc.url); fc.url = null; }
  $("#fcImg").removeAttribute("src");
});


/* ---------- length, disk, free up space ---------- */

const fmtGB = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : Math.round((b || 0) / 1e6) + " MB");
function diskStat() {
  const d = S.disk;
  if (!d) return "";
  return `<span class="stat ${d.low ? "warn" : ""}" title="Free space on the clips disk. Auto-clipping pauses below ${d.min_free_gb} GB.">`
    + `<b>${d.free_gb} GB</b> free${d.low ? " · clipping paused" : ""}</span>`;
}

let cleanInfo = null;
const cleanWhich = () => [["posted", "#cleanPosted"], ["rejected", "#cleanRejected"]].filter(([, id]) => $(id).checked).map(([k]) => k);
async function openCleanup() {
  cleanInfo = null;
  $("#cleanGo").disabled = true;
  $("#cleanGo").textContent = "Delete files";
  $("#cleanPostedInfo").textContent = $("#cleanRejectedInfo").textContent = "Measuring…";
  $("#cleanDisk").textContent = S.disk ? `${S.disk.free_gb} GB free now. Auto-clipping pauses below ${S.disk.min_free_gb} GB.` : "";
  $("#cleanDialog").showModal();
  try {
    cleanInfo = await api("/api/cleanup", { dry: true });
    $("#cleanPostedInfo").textContent = `${cleanInfo.posted.clips} clip${cleanInfo.posted.clips === 1 ? "" : "s"} · ${fmtGB(cleanInfo.posted.bytes)}`;
    $("#cleanRejectedInfo").textContent = `${cleanInfo.rejected.clips} clip${cleanInfo.rejected.clips === 1 ? "" : "s"} · ${fmtGB(cleanInfo.rejected.bytes)}`;
    cleanUpdate();
  } catch (e) { $("#cleanPostedInfo").textContent = e.message; }
}
function cleanUpdate() {
  if (!cleanInfo) return;
  const w = cleanWhich();
  const n = w.reduce((a, k) => a + cleanInfo[k].clips, 0);
  const b = w.reduce((a, k) => a + cleanInfo[k].bytes, 0);
  $("#cleanGo").disabled = !n;
  $("#cleanGo").textContent = n ? `Delete ${n} clip${n === 1 ? "" : "s"} · ${fmtGB(b)}` : "Nothing to delete";
}
$("#cleanPosted").addEventListener("change", cleanUpdate);
$("#cleanRejected").addEventListener("change", cleanUpdate);
$("#cleanGo").addEventListener("click", async () => {
  const which = cleanWhich();
  if (!which.length) return;
  $("#cleanGo").disabled = true;
  try {
    const r = await api("/api/cleanup", { which, dry: false });
    $("#cleanDialog").close();
    toast(`Deleted ${r.files} files from ${r.clips} clip${r.clips === 1 ? "" : "s"} · freed ${fmtGB(r.bytes)}`);
    refresh();
  } catch (e) { toast(e.message); $("#cleanGo").disabled = false; }
});


/* ---------- "did you post this?" ---------- */
// asked for clips you kept or opened in Finder, until you answer (Not yet = ask again after the next Finder open)
function askPosted(m) {
  if (m.cleaned || !m.clip || m.delete_in != null) return false;
  if (["posted", "rejected", "clipping", "failed", "detected"].includes(m.status)) return false;
  if (m.status !== "kept" && !m.opened_at) return false;
  return !(m.asked_at && m.asked_at >= (m.opened_at || 0));
}
function openAsk() {
  const list = S.moments.filter(askPosted);
  $("#askList").innerHTML = list.map((m) => `
    <li>
      ${m.thumb ? `<img src="${esc(media(m.thumb))}" alt="" loading="lazy">` : `<span class="noimg"></span>`}
      <div class="who"><b>${esc(m.channel)}</b><small>${esc(m.hook || m.tag)} · ${ago(m.created)}${(m.edit_info?.kind || m.kind) === "long" ? " · long" : ""}</small></div>
      <button class="btn sm posted" data-askyes="${m.id}">Posted</button>
      <button class="btn sm ghost" data-askno="${m.id}">Not yet</button>
    </li>`).join("");
  $("#askAll").hidden = list.length < 2;
  $("#askDialog").showModal();
}


/* ---------- master switch ---------- */
async function setRunning(on) {
  ui.settingsDirty = Date.now();
  try {
    await api("/api/running", { on });
    toast(on ? "ClipWatch is on: recording, analyzing and clipping" : "ClipWatch is off: nothing is recorded or analyzed");
    refresh();
  } catch (err) { toast(err.message); }
}
$("#running").addEventListener("change", (e) => setRunning(e.target.checked));
$("#turnOn").addEventListener("click", () => setRunning(true));

/* ---------- Rules board: what gets clipped, per streamer ---------- */
const LEN_LABEL = { best: "Best 60–90 s", long: "Long 30–45 s", short: "Short 12–20 s", auto: "Auto length" };
const styleLabel = (k) => (S.styles || []).find((x) => x.key === k)?.label || k;
const LEN_SHORT = { best: "Best", long: "Long", short: "Short", auto: "Auto" };
function sel(field, value, options, defLabel) {
  return `<select data-f="${field}" aria-label="${field.replace("_", " ")}">`
    + (defLabel != null ? `<option value="" ${value == null ? "selected" : ""}>${esc(defLabel)}</option>` : "")
    + options.map(([v, l]) => `<option value="${v}" ${String(value) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")
    + `</select>`;
}
const onOff = (field, value, def) => sel(field, value == null ? null : (value ? "1" : "0"), [["1", "On"], ["0", "Off"]], `Default (${def ? "On" : "Off"})`);
const sw = (field, on, title = "") => `<label class="switch sm" title="${esc(title)}"><input type="checkbox" data-f="${field}" ${on ? "checked" : ""}><span class="track"><span class="knob"></span></span></label>`;
function defaultRow() {
  const d = S.settings;
  return `<tr class="def" data-key="__default">
    <th scope="row"><b>All streamers</b><small>defaults</small></th>
    <td class="muted">—</td>
    <td>${sw("auto_clip", d.auto_clip, "Clip when the score crosses the line")}</td>
    <td><input type="number" class="num" data-f="threshold" min="0.5" max="20" step="0.5" value="${d.threshold}" aria-label="Default score line"></td>
    <td>${sel("length", d.highlight_length, Object.entries(LEN_LABEL))}</td>
    <td>${sel("style", d.edit_style, (S.styles || []).map((x) => [x.key, x.label]))}</td>
    <td>${sw("auto_edit", d.auto_edit !== false, "Make the 9:16 edit right after the clip")}</td>
    <td><input type="number" class="num" data-f="max_per_hour" min="0" max="60" step="1" value="${d.max_per_hour || 0}" aria-label="Default max clips per hour"></td>
    <td class="muted">0 = no limit</td>
  </tr>`;
}
function ruleRow(r) {
  const o = r.opts || {}, d = S.settings;
  const now = r.watching
    ? `${r.status === "live" ? "Live" : esc(STATUS[r.status] || r.status)} · score <b>${r.score != null ? Number(r.score).toFixed(1) : "–"}</b> / ${r.rule.threshold} · ${r.clips_hour} clip${r.clips_hour === 1 ? "" : "s"} last hour`
    : r.live ? "Live, not recording" : r.listed ? "On the list" : "Offline";
  return `<tr data-key="${esc(r.key)}" class="${r.watching ? "rec" : ""} ${r.rule.record ? "" : "off"}">
    <th scope="row"><span class="pf ${r.platform}" title="${PF[r.platform] || ""}"></span><b>${esc(r.name)}</b>${r.fav ? `<small>favorite</small>` : ""}</th>
    <td>${sw("record", r.rule.record, r.live || r.watching ? "On: record now. Off: stop, and autopilot never picks them" : "Off: autopilot never picks them")}</td>
    <td>${onOff("auto_clip", o.auto_clip, d.auto_clip)}</td>
    <td><input type="number" class="num" data-f="threshold" min="0.5" max="20" step="0.5" value="${o.threshold ?? ""}" placeholder="${d.threshold}" aria-label="Score line for ${esc(r.name)}"></td>
    <td>${sel("length", o.length, Object.entries(LEN_LABEL), `Default (${LEN_SHORT[d.highlight_length] || d.highlight_length})`)}</td>
    <td>${sel("style", o.style, (S.styles || []).map((x) => [x.key, x.label]), `Default (${styleLabel(d.edit_style).split(" ")[0]})`)}</td>
    <td>${onOff("auto_edit", o.auto_edit, d.auto_edit !== false)}</td>
    <td><input type="number" class="num" data-f="max_per_hour" min="0" max="60" step="1" value="${o.max_per_hour ?? ""}" placeholder="${d.max_per_hour || 0}" aria-label="Max clips per hour for ${esc(r.name)}"></td>
    <td class="now">${now}</td>
  </tr>`;
}
function renderRules() {
  const body = $("#rulesBody");
  const rows = [defaultRow(), ...(S.board || []).map(ruleRow)];
  const keys = ["__default", ...(S.board || []).map((r) => r.key)].join("|");
  const editing = document.activeElement?.closest?.("#rulesBody tr") || null;
  if (body.dataset.keys !== keys) {
    if (editing) return;  // don't rebuild under your cursor; next refresh catches up
    body.innerHTML = rows.join("");
    body.dataset.keys = keys;
    [...body.children].forEach((tr, i) => { tr.dataset.sig = rows[i]; });
    return;
  }
  [...body.children].forEach((tr, i) => {
    if (tr === editing || tr.dataset.sig === rows[i]) return;
    const fresh = document.createRange().createContextualFragment(rows[i]).firstElementChild;
    fresh.dataset.sig = rows[i];
    tr.replaceWith(fresh);
  });
}
$("#rulesBody").addEventListener("change", async (e) => {
  const el = e.target.closest("[data-f]");
  if (!el) return;
  const key = el.closest("tr").dataset.key, f = el.dataset.f;
  let v = el.type === "checkbox" ? el.checked : el.value === "" ? null : el.type === "number" ? Number(el.value) : el.value;
  if (el.tagName === "SELECT" && (f === "auto_clip" || f === "auto_edit") && v !== null) v = v === "1";
  try {
    if (key === "__default") {
      if (v === null) return;
      const cfgKey = { auto_clip: "auto_clip", threshold: "threshold", length: "highlight_length", style: "edit_style", auto_edit: "auto_edit", max_per_hour: "max_per_hour" }[f];
      ui.settingsDirty = Date.now();
      await api("/api/settings", { [cfgKey]: v });
      toast("Default saved: every streamer without their own value uses it");
    } else {
      const r = (S.board || []).find((x) => x.key === key) || {};
      await api("/api/rules", { key, [f]: v, name: r.name, platform: r.platform, url: r.url, live: r.live });
      if (f === "record") toast(v ? (r.live ? `Recording ${r.name}` : `${r.name} can be recorded again`) : `${r.name}: stopped, autopilot skips them`);
      else toast(`${r.name}: saved`);
    }
    el.blur();
    refresh();
  } catch (err) { toast(err.message); }
});


/* ---------- tell Claude how to edit a clip ---------- */
document.addEventListener("submit", async (e) => {
  const f = e.target.closest?.(".ai-form");
  if (!f) return;
  e.preventDefault();
  const text = f.q.value.trim();
  if (!text) return;
  try {
    await api("/api/ai-edit", { id: +f.dataset.mid, text });
    f.q.value = "";
    f.q.blur();
    toast("Claude is planning the edit; the clip re-renders when it's done");
    refresh();
  } catch (err) { toast(err.message); }
});
// keep an open ideas list open when the card refreshes
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (!d.matches?.("details.ideas")) return;
  const id = +d.closest(".moment").dataset.mid;
  if (d.open) ui.openIdeas.add(id); else ui.openIdeas.delete(id);
}, true);
