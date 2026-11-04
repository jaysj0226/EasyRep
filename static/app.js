"use strict";
const $ = id => document.getElementById(id);
const enc = encodeURIComponent;
const roles = {admin:"전체 관리자",operator:"매장 운영자",viewer:"조회 전용"};
const qualities = {valid:"유효",invalid:"측정 오류",crc_error:"검사 오류",disconnected:"연결 끊김"};
const statuses = {
  pending:"접수 대기",sending:"전송 중",publishing:"전송 중",published:"게이트웨이 전송",
  accepted:"요청 접수",requested:"노드 요청 수신",applied:"장치 적용 확인",rejected:"거절",
  interrupted:"처리 중단",execution_unverified:"실행 미확인",expired:"기한 만료",unknown:"전달 여부 미확인",not_sent:"미전송"
};
const terminal = new Set(["applied","rejected","expired","interrupted","execution_unverified","not_sent"]);
const labels = {T1:"천장 온도",T2:"ㄱ 모서리 온도",T3:"ㄷ 모서리 온도",H1:"상대습도",RH1:"상대습도"};
const reasons = {hardware_not_commissioned:"장치 출력 미승인",node_offline:"노드 오프라인",missing_telemetry:"측정값 없음",stale_telemetry:"측정 지연",transport_offline:"전송 연결 끊김",gateway_unhealthy:"게이트웨이 이상",clock_error:"장치 시계 오류"};
let session = null, store = "", cageId = "", cages = [], view = 0, fetchVersion = 0, commandBusy = false;
let onlyCare = false;
const tabs = ["sensors","controls","history"];

function tag(name, text, className) {
  const el = document.createElement(name);
  if (text != null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}
function mapped(table,key) { return Object.hasOwn(table,key) ? table[key] : String(key || "미확인"); }
function badge(text, tone) { return tag("span",text,"badge " + (tone || "")); }
function empty(text) { return tag("p",text,"empty"); }
function displayTime(value) {
  const date = new Date(typeof value === "number" && value < 1e11 ? value * 1000 : value);
  return value != null && !Number.isNaN(date.valueOf()) ?
    date.toLocaleString("ko-KR",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}) : "기록 없음";
}
function sampledRecently(sample) {
  return [sample?.sampled_at,sample?.received_at].every(time => {
    const age = Date.now() - Date.parse(time);
    return age >= -2000 && age <= 60000;
  });
}
function missingBB6(readings) {
  const missing = ["T1","T2","T3"].filter(id => !Object.hasOwn(readings,id));
  if (!Object.hasOwn(readings,"H1") && !Object.hasOwn(readings,"RH1")) missing.push("H1");
  return missing;
}
function condition(cage) {
  const t = cage.telemetry;
  if (cage.transport_connected !== true) return ["전송 끊김","bad"];
  if (!t) return ["측정 없음","warn"];
  const prefix = t.data_source === "simulation" ? "모의 · " : "";
  if (!sampledRecently(t)) return [prefix + "시각 확인","warn"];
  if (missingBB6(t.readings || {}).length) return [prefix + "일부 센서 누락","warn"];
  const readings = Object.values(t.readings || {});
  if (!readings.length || readings.some(r => r?.quality !== "valid" || !Number.isFinite(r.value))) return [prefix + "센서 확인","warn"];
  return [prefix + "최근 측정","good"];
}
function sensorName(id) {
  return Object.hasOwn(labels,id) ? id + "(" + labels[id] + ")" : id;
}
function blockedReason(reason) {
  const [kind,id] = String(reason).split(":");
  const sensorReasons = {
    sensor_unavailable:"측정값을 확인할 수 없습니다",
    sensor_out_of_range:"값이 장치에 설정된 범위를 벗어났습니다",
    guard_minimum:"값이 장치에 설정된 하한 조건에 걸렸습니다",
    guard_maximum:"값이 장치에 설정된 상한 조건에 걸렸습니다"
  };
  if (id && Object.hasOwn(sensorReasons,kind)) return sensorName(id) + " " + sensorReasons[kind];
  return mapped(reasons,reason);
}
function cageAdvice(cage) {
  if (!cage) return cages.length ? "검색어나 필터를 바꾸어 표시할 사육장을 선택하세요." : "매장과 사육장을 선택하면 측정 상태를 확인할 수 있습니다.";
  const notes = [];
  if (cage.transport_connected !== true) notes.push("전송 연결이 끊겼습니다. 현장 Pi와 장치 연결을 확인하세요.");
  const t = cage.telemetry;
  if (!t) {
    notes.push("온습도 측정값을 아직 받지 못했습니다. BB6 센서와 수신 상태를 확인하세요.");
    return notes.join(" ");
  }
  if (t.data_source === "simulation") notes.push("모의 데이터입니다.");
  const readings = t.readings || {};
  const missing = missingBB6(readings);
  if (missing.length) notes.push(missing.map(sensorName).join(", ") + " 값이 누락되었습니다.");
  const invalid = Object.entries(readings).filter(([,r]) => r?.quality !== "valid" || !Number.isFinite(r.value));
  if (invalid.length) notes.push(invalid.map(([id,r]) => sensorName(id) + " " + (r?.quality === "valid" ? "수치 오류" : mapped(qualities,r?.quality))).join(", ") + ". 센서 배선과 연결을 확인하세요.");
  if (!sampledRecently(t)) notes.push("측정 시각 또는 Pi 수신 시각이 최근 60초 범위 밖이거나 확인되지 않습니다. 장치 시계와 수신 상태를 확인하세요.");
  if (cage.transport_connected === true && !missing.length && !invalid.length && sampledRecently(t)) notes.push("필수 온습도 채널의 유효한 측정값이 최근 60초 안에 수신되었습니다.");
  return notes.join(" ");
}
function selectTab(name, focus = false) {
  for (const key of tabs) {
    const selected = key === name, tab = $("tab-" + key);
    tab.setAttribute("aria-selected",String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $("panel-" + key).hidden = !selected;
  }
  if (focus) $("tab-" + name).focus();
}
function resetFilters() {
  onlyCare = false; $("cage-search").value = "";
  $("filter-all").setAttribute("aria-pressed","true");
  $("filter-care").setAttribute("aria-pressed","false");
}
function filteredCages() {
  const query = $("cage-search").value.trim().toLocaleLowerCase();
  return cages.filter(c => (!onlyCare || condition(c)[1] !== "good") &&
    [c.cage_id,c.node_id].some(id => String(id || "").toLocaleLowerCase().includes(query)));
}
function updateFilteredCages() {
  const shown = filteredCages();
  if (!shown.some(c => c.cage_id === cageId)) {
    setCage(shown[0]?.cage_id || "");
    return;
  }
  renderCages(); renderDetails();
}
function showNotice(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
function showLogin(message = "") {
  session = null; store = ""; cageId = ""; cages = []; view++; fetchVersion++;
  resetFilters(); selectTab("sensors");
  $("boot").hidden = true; $("console").hidden = true; $("access").hidden = false;
  $("password").value = ""; $("login-message").textContent = message; $("username").focus();
}
async function api(path, data) {
  const options = {credentials:"same-origin",cache:"no-store",signal:AbortSignal.timeout(15000)};
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type":"application/json"};
    if (session?.csrf) options.headers["X-CSRF-Token"] = session.csrf;
    options.body = JSON.stringify(data);
  }
  let response;
  try { response = await fetch(path,options); }
  catch { throw new Error("서버에서 응답을 받지 못했습니다."); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && session) showLogin("세션이 만료되었습니다. 다시 로그인하세요.");
    const err = new Error(body.error || "요청을 처리하지 못했습니다.");
    err.data = body; err.status = response.status; throw err;
  }
  return body;
}
const storePath = () => "/api/stores/" + enc(store);
function setCage(id) {
  const moveDetailFocus = id !== cageId && tabs.some(name =>
    document.activeElement === $("tab-" + name) || $("panel-" + name).contains(document.activeElement));
  if (id !== cageId) { $("action-message").textContent = ""; selectTab("sensors"); }
  cageId = id; view++; renderCages(); renderDetails();
  if (moveDetailFocus) (id ? $("tab-sensors") : $("cage-search")).focus();
  $("commands").replaceChildren(empty("제어 기록을 불러오는 중…"));
  refreshHistory(view);
}
function renderCages() {
  const focusedCage = $("cages").contains(document.activeElement) ? document.activeElement.dataset.cage : null;
  const good = cages.filter(c => condition(c)[1] === "good").length;
  $("count-all").textContent = cages.length; $("count-ok").textContent = good; $("count-care").textContent = cages.length-good;
  const shown = filteredCages();
  $("filter-all").setAttribute("aria-pressed",String(!onlyCare));
  $("filter-care").setAttribute("aria-pressed",String(onlyCare));
  $("list-summary").textContent = "전체 " + cages.length + "개 중 " + shown.length + "개 표시";
  const nodes = shown.map(c => {
    const b = tag("button",null,"cage-button");
    b.dataset.cage = c.cage_id;
    b.setAttribute("aria-pressed",String(c.cage_id === cageId));
    b.append(tag("b",c.cage_id),badge(...condition(c)));
    b.addEventListener("click",() => setCage(c.cage_id)); return b;
  });
  $("cages").replaceChildren(...(nodes.length ? nodes : [empty(cages.length ? "검색·필터 조건에 맞는 사육장이 없습니다." : "표시할 사육장이 없습니다.")]));
  if (focusedCage) (nodes.find(n => n.dataset.cage === focusedCage) || nodes.find(n => n.dataset.cage === cageId) || $("cage-search")).focus();
}
function renderDetails(keepInput = false) {
  const cage = cages.find(c => c.cage_id === cageId);
  $("cage-title").textContent = cage ? cageId : "사육장 상세";
  $("cage-state").replaceChildren(...(cage ? [badge(...condition(cage))] : []));
  $("cage-advice").setAttribute("role","status");
  $("cage-advice").dataset.tone = cage ? condition(cage)[1] : "";
  const advice = cageAdvice(cage);
  if ($("cage-advice").textContent !== advice) $("cage-advice").textContent = advice;
  $("cage-meta").textContent = cage ? [cage.node_id, cage.mode === "auto" ? "자동 모드" : cage.mode === "manual" ? "수동 모드" : cage.mode].filter(Boolean).join(" · ") : "매장과 사육장을 선택하세요.";
  const t = cage?.telemetry;
  const readings = t?.readings || {};
  const channels = [...Object.entries(readings), ...missingBB6(readings).map(key => [key,null])];
  const tiles = (cage ? channels : []).map(([key,r]) => {
    const valid = r?.quality === "valid" && Number.isFinite(r.value);
    const unit = /^T\d+$/.test(key) ? " °C" : /^R?H\d+$/.test(key) ? " %" : /^LUX\d+$/.test(key) ? " lx" : "";
    const tile = tag("div",null,"reading");
    tile.dataset.quality = valid ? "valid" : "error";
    if (valid && /^R?H\d+$/.test(key)) tile.dataset.kind = "humidity";
    tile.append(tag("b",Object.hasOwn(labels,key) ? key + " · " + labels[key] : key),
      tag("strong",valid ? r.value.toLocaleString("ko-KR",{maximumFractionDigits:2}) + unit : "—"),
      tag("small",r ? mapped(qualities,r.quality) : "미수신"));
    return tile;
  });
  $("readings").replaceChildren(...(tiles.length ? tiles : [empty("수신된 센서값이 없습니다.")]));
  $("sample-meta").textContent = t ? (t.data_source === "simulation" ? "모의 측정 · " : "") + "측정 " + displayTime(t.sampled_at) + " · Pi 수신 " + displayTime(t.received_at) + " · 최근 측정 기준 60초" : "";
  if (!keepInput) renderControls(cage);
}
function renderControls(cage) {
  const previous = new Map([...$("controls").querySelectorAll("input")].map(i => [i.dataset.device,i.value]));
  const hasAccess = ["operator","admin"].includes(session?.user.role);
  $("control-note").textContent = hasAccess ? "장치를 선택하면 전송 직전에 매장과 동작을 다시 확인합니다." : "이 계정은 장치 상태와 제어 기록을 조회할 수 있습니다.";
  const devices = Object.entries(cage?.actuators || {}).map(([id,a]) => {
    const section = tag("div",null,"device"), head = tag("div",null,"device-head"), obs = a.observed;
    head.append(tag("h3",id),badge(typeof obs?.state === "boolean" ? (obs.state ? "마지막 관측 켜짐" : "마지막 관측 꺼짐") : "관측 없음"));
    section.append(head);
    if (obs?.occurred_at) section.append(tag("p","장치 관측 " + displayTime(obs.occurred_at)));
    if (a.on_blocked_reason) section.append(tag("p","켜기 제한 · " + blockedReason(a.on_blocked_reason)));
    if (!hasAccess) return section;
    const actions = tag("div",null,"control-actions"), input = tag("input"), label = tag("label","켜짐 시간(초) ");
    input.type = "number"; input.step = "any"; input.min = "0.001"; input.max = String(Math.min(3600,Number(a.max_on_s) || 0));
    input.dataset.device = id; input.value = previous.get(id) || String(Math.min(1,Number(input.max)));
    label.append(input);
    for (const [key,title] of [["on","켜기"],["off","끄기"]]) {
      const b = tag("button",title,key === "on" ? "primary" : "");
      b.disabled = commandBusy || a.enabled === false || !cage.transport_connected || cage.mode !== "manual" || (key === "on" && (a.on_blocked_reason !== null || +input.max <= 0));
      if (key === "on") actions.append(label);
      b.addEventListener("click",() => {
        if (key === "on" && (!input.value || !input.reportValidity())) return;
        sendCommand(cage.cage_id,id,key,key === "on" ? Number(input.value) : 0);
      });
      actions.append(b);
    }
    section.append(actions); return section;
  });
  $("controls").replaceChildren(...(devices.length ? devices : [empty("이 사육장에 등록된 제어 장치가 없습니다.")]));
}
function requestKey(user,shop,cage,device,action) {
  return "easyrep-pending/" + [user,shop,cage,device,action].map(enc).join("/");
}
function historyRow(c) {
  const row = tag("div",null,"log-row"), left = tag("div"), right = tag("div",null,"log-status");
  left.append(tag("b",c.actuator_id + " · " + (c.action === "on" ? c.duration_s + "초 켜기" : "끄기")),
    tag("small",displayTime(c.created_at) + " · " + c.username));
  right.append(badge(mapped(statuses,c.status),c.status === "applied" ? "good" : ["rejected","expired","interrupted"].includes(c.status) ? "bad" : "warn"));
  if (terminal.has(c.status)) {
    const key = requestKey(c.username,store,c.cage_id,c.actuator_id,c.action);
    if (sessionStorage.getItem(key) === c.request_id) sessionStorage.removeItem(key);
  }
  if (!terminal.has(c.status) || c.status === "expired" || c.status === "interrupted") {
    const button = tag("button","응답 조회","quiet");
    button.addEventListener("click",async () => {
      const token = view; button.disabled = true;
      try { await api(storePath() + "/commands/" + enc(c.request_id)); if (view === token) await refreshHistory(token,false); }
      catch(e) { if (view === token) showNotice(e.message); }
      finally { button.disabled = false; }
    }); right.append(button);
  }
  row.append(left,right); return row;
}
async function refreshHistory(token, poll = true) {
  if (!store || !cageId) { $("commands").replaceChildren(empty("사육장을 선택하면 기록을 볼 수 있습니다.")); return; }
  const base = storePath(), cage = cageId;
  try {
    const data = await api(base + "/commands?cage_id=" + enc(cage));
    if (view !== token) return;
    const entries = data.commands;
    const pending = entries.filter(c => !terminal.has(c.status)).slice(0,3);
    if (poll && pending.length) {
      await Promise.all(pending.map(async c => {
        try { const out = await api(base + "/commands/" + enc(c.request_id)); Object.assign(c,out.command); }
        catch { /* The last recorded state remains visible until the next lookup. */ }
      }));
    }
    if (view !== token) return;
    $("commands").replaceChildren(...(entries.length ? entries.map(historyRow) : [empty("아직 제어 요청이 없습니다.")]));
  } catch(e) { if (view === token) $("commands").replaceChildren(empty(e.message)); }
}
async function sendCommand(cage,device,action,seconds) {
  if (commandBusy || cageId !== cage) return;
  const title = $("store").selectedOptions[0]?.textContent || store;
  if (!crypto.randomUUID) { $("action-message").textContent = "장치 제어에는 HTTPS 또는 localhost 접속이 필요합니다."; return; }
  const token = view, path = storePath(); commandBusy = true; renderControls(cages.find(c => c.cage_id === cage));
  const key = requestKey(session.user.username,store,cage,device,action);
  let previous;
  try {
    previous = sessionStorage.getItem(key);
    if (previous) {
      try {
        const prior = await api(path + "/commands/" + enc(previous));
        if (view === token) $("action-message").textContent = "직전 요청 · " + mapped(statuses,prior.command.status) + ". 새 명령을 전송하지 않았습니다.";
        if (terminal.has(prior.command.status)) sessionStorage.removeItem(key);
        return;
      } catch(e) { if (e.status !== 404) throw e; }
    }
    if (view !== token || !window.confirm(title + " / " + cage + "\n" + device + " " + (action === "on" ? seconds + "초 켜기" : "끄기") + " 요청을 보낼까요?")) return;
    const payload = {request_id:previous || crypto.randomUUID(),actuator_id:device,action,duration_s:seconds};
    sessionStorage.setItem(key,payload.request_id);
    const out = await api(path + "/cages/" + enc(cage) + "/commands",payload);
    if (terminal.has(out.command.status)) sessionStorage.removeItem(key);
    if (view === token) $("action-message").textContent = device + " · " + mapped(statuses,out.command.status);
  } catch(e) {
    if (e.data?.command && terminal.has(e.data.command.status)) sessionStorage.removeItem(key);
    if (view === token) $("action-message").textContent = e.data?.command ? device + " · " + mapped(statuses,e.data.command.status) + " · " + e.message : e.message + " 제어 기록에서 결과를 확인하세요.";
  } finally {
    commandBusy = false;
    if (session) renderControls(cages.find(c => c.cage_id === cageId));
    if (view === token) await refreshHistory(token);
  }
}
async function refresh() {
  if (!session || !store) return;
  const currentStore = store, version = ++fetchVersion;
  $("refresh").disabled = true;
  try {
    const data = await api(storePath() + "/cages");
    if (version !== fetchVersion || currentStore !== store) return;
    cages = data.cages;
    const simulated = cages.length > 0 && cages.every(c => c.transport === "simulated");
    showNotice(); $("updated").textContent = (simulated ? "모의 매장 · " : "") + "마지막 조회 " + displayTime(Date.now()) + " · 15초마다 갱신";
    if (!filteredCages().some(c => c.cage_id === cageId)) { setCage(filteredCages()[0]?.cage_id || ""); }
    else {
      renderCages();
      renderDetails($("controls").contains(document.activeElement));
      await refreshHistory(view);
    }
  } catch(e) {
    if (version !== fetchVersion || currentStore !== store) return;
    view++; cages = []; cageId = "";
    renderCages(); renderDetails(); $("commands").replaceChildren(empty("현재 상태를 조회할 수 없습니다."));
    for (const id of ["count-all","count-ok","count-care"]) $(id).textContent = "—";
    $("updated").textContent = "매장 데이터 조회 실패"; showNotice(e.message);
  } finally { if (version === fetchVersion) $("refresh").disabled = false; }
}
function selectStore() {
  view++; fetchVersion++; store = $("store").value; cageId = ""; cages = []; showNotice();
  resetFilters(); selectTab("sensors");
  $("action-message").textContent = ""; $("updated").textContent = store ? "매장 데이터 조회 중…" : "등록된 매장이 없습니다.";
  renderCages(); renderDetails(); $("commands").replaceChildren();
  for (const id of ["count-all","count-ok","count-care"]) $(id).textContent = "—";
  $("refresh").disabled = !store; refresh();
}
async function openConsole(auth) {
  session = auth; const token = ++view;
  const data = await api("/api/stores"); if (view !== token) return;
  $("access").hidden = true; $("boot").hidden = true; $("console").hidden = false;
  $("account").textContent = auth.user.username + " · " + mapped(roles,auth.user.role);
  $("store").replaceChildren(...data.stores.map(s => new Option(s.name + " (" + s.id + ")",s.id)));
  selectStore();
}
$("login-form").addEventListener("submit",async event => {
  event.preventDefault(); $("login-button").disabled = true; $("login-message").textContent = "";
  try {
    const auth = await api("/api/login",{username:$("username").value.trim(),password:$("password").value});
    $("password").value = ""; await openConsole(auth);
  } catch(e) { showLogin(e.status === 401 ? "계정 이름 또는 비밀번호를 확인하세요." : e.message); }
  finally { $("login-button").disabled = false; }
});
$("logout").addEventListener("click",async () => {
  try { await api("/api/logout",{}); showLogin(); }
  catch(e) { if (session) showNotice(e.message); }
});
$("store").addEventListener("change",selectStore);
$("refresh").addEventListener("click",refresh);
$("cage-search").addEventListener("input",() => updateFilteredCages());
for (const [id,care] of [["filter-all",false],["filter-care",true]]) {
  $(id).addEventListener("click",() => { onlyCare = care; updateFilteredCages(); });
}
for (const [index,name] of tabs.entries()) {
  const tab = $("tab-" + name);
  tab.addEventListener("click",() => selectTab(name));
  tab.addEventListener("keydown",event => {
    const keys = {ArrowLeft:(index + tabs.length - 1) % tabs.length,ArrowRight:(index + 1) % tabs.length,Home:0,End:tabs.length - 1};
    if (!Object.hasOwn(keys,event.key)) return;
    const next = keys[event.key];
    event.preventDefault(); selectTab(tabs[next],true);
  });
}
selectTab("sensors");
document.addEventListener("visibilitychange",() => {
  if (!document.hidden && session && !commandBusy && !$("refresh").disabled) refresh();
});
setInterval(() => { if (!document.hidden && session && !$("refresh").disabled && !commandBusy) refresh(); },15000);
api("/api/session").then(openConsole).catch(e => showLogin(e.status === 401 ? "" : e.message));
