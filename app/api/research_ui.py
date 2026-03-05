from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(prefix="/research")


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def research_ui() -> HTMLResponse:
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MemoMate Research UI</title>
  <script src="https://cdn.jsdelivr.net/npm/cytoscape@3.31.2/dist/cytoscape.min.js"></script>
  <style>
    :root{
      --bg:#f4f2ee;--panel:#ffffff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;
      --accent:#0f766e;--accent-2:#1d4ed8;--accent-3:#7c3aed;
      --warn:#b45309;--danger:#b91c1c;--shadow:0 10px 30px rgba(15,23,42,0.08);
      --log-h:220px;--splitter-h:6px;
    }
    *{box-sizing:border-box}
    body{margin:0;background:radial-gradient(circle at 10% 10%, #fff9f0, var(--bg) 60%);color:var(--ink);font-family:"Source Han Sans SC","Noto Sans SC","IBM Plex Sans",sans-serif}
    .layout{display:grid;grid-template-columns:380px 1fr;min-height:100vh}
    .left{background:var(--panel);border-right:1px solid var(--line);padding:16px;overflow:auto}
    .right{display:grid;grid-template-rows:1fr var(--splitter-h) var(--log-h);height:100vh}
    .section{margin-bottom:14px;padding:12px;border:1px solid var(--line);border-radius:14px;background:#fff;box-shadow:var(--shadow)}
    .section h3{margin:0 0 8px 0;font-size:14px;color:#0f172a;letter-spacing:0.2px}
    .row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
    .row>input,.row>select,.row>textarea{flex:1;padding:8px 10px;border:1px solid #d6d3ce;border-radius:10px;background:#fff}
    textarea{min-height:72px;resize:vertical}
    button{padding:7px 12px;border:0;background:var(--accent);color:#fff;border-radius:10px;cursor:pointer;font-weight:600}
    button.alt{background:#6b7280}
    button.ghost{background:#fff;color:#111827;border:1px solid #d1d5db}
    .small{font-size:12px;color:var(--muted)}
    .badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;background:#eef2ff;color:#1d4ed8;margin-left:6px}
    .badge.ok{background:#ecfdf3;color:#047857}
    .badge.warn{background:#fff7ed;color:#9a3412}
    .badge.fail{background:#fef2f2;color:#b91c1c}
    .task-item{padding:8px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;cursor:pointer;background:#fafafa}
    .task-item.active{border-color:#1d4ed8;background:#eef2ff}
    .task-title{font-size:13px;font-weight:600}
    .task-meta{font-size:11px;color:var(--muted)}
    .directions{display:flex;flex-direction:column;gap:6px}
    .dir-item{display:flex;gap:8px;align-items:center;padding:6px 8px;border:1px solid #e5e7eb;border-radius:10px;background:#fcfcfc;cursor:pointer}
    .dir-index{min-width:26px;height:26px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:#1d4ed8;color:#fff;font-size:12px}
    .dir-name{font-size:12px;color:#111827}

    .graph-wrap{position:relative;background:linear-gradient(180deg,#fffaf0 0%, #f6f4f1 50%, #f3f2ef 100%);border-bottom:1px solid var(--line)}
    #cy{width:100%;height:100%}
    .detail-card{position:absolute;top:12px;right:12px;width:340px;max-height:calc(100% - 24px);overflow:auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;box-shadow:var(--shadow);padding:12px;display:none}
    .detail-card.show{display:block}
    .detail-title{font-weight:700;font-size:14px;margin-bottom:6px}
    .detail-line{font-size:12px;color:var(--muted);margin-bottom:6px}
    .detail-block{font-size:12px;line-height:1.5;margin-bottom:8px}
    .detail-block .label{color:#6b7280;font-weight:600;margin-right:6px}
    .detail-actions{display:flex;gap:8px;margin-top:8px}
    .pill{display:inline-flex;align-items:center;border:1px solid #e5e7eb;border-radius:999px;padding:2px 8px;font-size:11px;color:#374151;background:#f9fafb;margin-right:6px}

    .splitter{cursor:row-resize;background:linear-gradient(90deg,#f0f0f0,#d1d5db,#f0f0f0)}

    .log-panel{display:flex;flex-direction:column;background:#fff}
    .log-head{display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}
    .log-list{flex:1;overflow:auto;padding:8px 10px;font-size:12px;line-height:1.5;white-space:pre-wrap}
    .log-item{margin-bottom:6px}
    .log-item.error{color:var(--danger)}

    .toggle{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#374151;cursor:pointer}
    .toggle input{width:14px;height:14px}

    .status-bar{position:absolute;left:12px;top:12px;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:6px 10px;font-size:12px;box-shadow:var(--shadow)}
    .legend{position:absolute;left:12px;bottom:12px;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:6px 10px;font-size:11px;box-shadow:var(--shadow);display:flex;gap:10px;align-items:center}
    .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
  </style>
</head>
<body>
  <div class="layout">
    <div class="left">
      <div class="section">
        <h3>用户切换（本地）</h3>
        <div class="row">
          <select id="userSelect"></select>
          <button class="alt" onclick="switchUser()">切换</button>
        </div>
        <div class="row">
          <input id="userInput" placeholder="输入 wecom_user_id，如 local-dev"/>
          <button class="ghost" onclick="switchUser(true)">使用输入</button>
        </div>
        <div class="small" id="currentUser">当前用户：-</div>
      </div>

      <div class="section">
        <h3>新建调研任务</h3>
        <div class="row"><input id="topic" placeholder="输入主题，例如 ultrasound report generation"/></div>
        <div class="row"><button onclick="createTask()">创建并规划</button></div>
        <div class="small">方向规划偏“解决方案流派”，如生成式/检索增强/模板/多阶段等。</div>
      </div>

      <div class="section">
        <h3>任务列表</h3>
        <div class="row">
          <button class="alt" onclick="refreshTasks()">刷新任务</button>
          <label class="toggle"><input type="checkbox" id="autoRefreshTasks" checked/>自动刷新</label>
        </div>
        <div id="tasks" class="small">加载中...</div>
      </div>

      <div class="section">
        <h3>方向列表</h3>
        <div id="directions" class="directions small">暂无方向</div>
      </div>

      <div class="section">
        <h3>轮次探索</h3>
        <div class="row"><input id="directionIndex" placeholder="方向序号，如 1"/><button onclick="startExplore()">开始</button></div>
        <div class="row">
          <select id="action">
            <option value="expand">拓展 · 扩大邻域</option>
            <option value="deepen">深化 · 更细问题</option>
            <option value="pivot">转向 · 换方向</option>
            <option value="converge">收敛 · 聚焦核心</option>
            <option value="stop">停止 · 结束本轮</option>
          </select>
        </div>
        <div id="actionHint" class="small">主流程：输入自然语言并点击“继续调研”。</div>
        <div class="row"><textarea id="feedback" placeholder="输入你的反馈，如：关注 hallucination 评估"></textarea></div>
        <div class="row">
          <button onclick="continueByIntent()">继续调研</button>
          <button class="ghost" onclick="propose()">生成候选</button>
        </div>
        <div id="candidates" class="small"></div>
      </div>

      <div class="section">
        <h3>视图控制</h3>
        <div class="row">
          <label class="toggle"><input type="checkbox" id="togglePapers"/>显示论文节点</label>
        </div>
        <div class="row">
          <input id="paperLimit" type="number" min="1" max="50" value="8" style="width:90px"/>
          <label class="toggle"><input type="checkbox" id="toggleLock" checked/>锁定布局</label>
          <button class="ghost" onclick="reflowLayout()">重排布局</button>
        </div>
        <div class="row">
          <label class="toggle"><input type="checkbox" id="autoRefreshGraph"/>自动刷新图谱</label>
        </div>
      </div>

      <div class="section">
        <h3>已保存论文</h3>
        <div id="savedPapers" class="small">暂无</div>
      </div>
    </div>

    <div class="right">
      <div class="graph-wrap">
        <div id="cy"></div>
        <div class="status-bar" id="statusBar">任务未选择</div>
        <div class="legend">
          <span><i style="background:#0f766e"></i>主题</span>
          <span><i style="background:#1d4ed8"></i>方向</span>
          <span><i style="background:#7c3aed"></i>轮次</span>
          <span><i style="background:#d97706"></i>论文</span>
        </div>
        <div class="detail-card" id="detailCard"></div>
      </div>
      <div class="splitter" id="splitter"></div>
      <div class="log-panel">
        <div class="log-head">
          <span>日志</span>
          <label class="toggle"><input type="checkbox" id="autoScroll" checked/>自动滚动</label>
          <button class="ghost" onclick="refreshTokenManual()">刷新登录</button>
          <button class="ghost" onclick="clearLog()">清空</button>
        </div>
        <div class="log-list" id="logList"></div>
      </div>
    </div>
  </div>

<script>
let currentTask = "";
let currentRound = null;
let currentPaperToken = "";
let taskCache = [];
let includePapers = false;
let layoutLocked = true;
let autoRefreshGraph = false;
let autoRefreshTasks = true;
let autoScroll = true;
let lastRenderNodes = [];
let lastRenderEdges = [];
let paperLimit = 8;
let currentUserId = localStorage.getItem("memomate_dev_user_id") || "local-dev";

function getAccessToken(){
  return localStorage.getItem("memomate_access_token") || "";
}
function setTokens(data){
  if(data && data.access_token){
    localStorage.setItem("memomate_access_token", data.access_token);
  }
  if(data && data.refresh_token){
    localStorage.setItem("memomate_refresh_token", data.refresh_token);
  }
}
function getHeaders(){
  const token = getAccessToken();
  return token ? {Authorization:`Bearer ${token}`} : {};
}
function setCurrentUserUI(){
  document.getElementById("currentUser").textContent = `当前用户：${currentUserId}`;
}
async function refreshToken(wecomUserId=null){
  const uid = (wecomUserId || currentUserId || "local-dev").trim();
  const resp = await fetch(`/api/v1/dev/token?wecom_user_id=${encodeURIComponent(uid)}`, {method:"POST"});
  const text = await resp.text();
  if(!resp.ok){
    throw new Error(text || resp.statusText);
  }
  const data = text ? JSON.parse(text) : {};
  setTokens(data);
  currentUserId = uid;
  localStorage.setItem("memomate_dev_user_id", uid);
  setCurrentUserUI();
  return data.access_token || "";
}
async function ensureToken(){
  if(getAccessToken()){
    return getAccessToken();
  }
  return await refreshToken();
}
async function refreshTokenManual(){
  try{
    await refreshToken();
    log("已刷新登录 token");
    await refreshTasks();
  }catch(e){
    log(`刷新 token 失败: ${e}`, "error");
  }
}
async function loadDevUsers(){
  try{
    const data = await api("/api/v1/dev/users?limit=120");
    const users = data.users || [];
    const select = document.getElementById("userSelect");
    if(!users.includes(currentUserId)){
      users.unshift(currentUserId);
    }
    select.innerHTML = users.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join("");
    select.value = currentUserId;
    setCurrentUserUI();
  }catch(e){
    log(`加载用户列表失败: ${e}`, "error");
  }
}
async function switchUser(fromInput=false){
  const inputVal = (document.getElementById("userInput").value || "").trim();
  const selected = document.getElementById("userSelect").value || "";
  const target = fromInput ? inputVal : selected;
  if(!target){
    log("请输入或选择用户 ID", "error");
    return;
  }
  try{
    await refreshToken(target);
    currentTask = "";
    currentRound = null;
    taskCache = [];
    document.getElementById("tasks").textContent = "加载中...";
    document.getElementById("directions").textContent = "暂无方向";
    document.getElementById("savedPapers").textContent = "暂无";
    await loadDevUsers();
    await refreshTasks();
    log(`已切换用户 ${target}`);
  }catch(e){
    log(`切换用户失败: ${e}`, "error");
  }
}
const cy = cytoscape({
  container: document.getElementById("cy"),
  elements: [],
  boxSelectionEnabled: true,
  selectionType: 'additive',
  minZoom: 0.2,
  maxZoom: 2.0,
  motionBlur: false,
  textureOnViewport: true,
  style: [
    {selector:'node',style:{'label':'data(label)','font-size':11,'text-wrap':'wrap','text-max-width':120,'background-color':'#8b8b8b','color':'#111827','text-outline-width':1.2,'text-outline-color':'#f8fafc','width':22,'height':22,'text-valign':'center','text-halign':'center'}},
    {selector:'node[type="topic"]',style:{'background-color':'#0f766e','color':'#fff','width':42,'height':42,'font-size':12}},
    {selector:'node[type="direction"]',style:{'background-color':'#1d4ed8','color':'#fff','width':34,'height':34,'font-size':12}},
    {selector:'node[type="round"]',style:{'background-color':'#7c3aed','color':'#fff','width':28,'height':28}},
    {selector:'node[type="paper"]',style:{'background-color':'#d97706','color':'#1f2937','width':20,'height':20}},
    {selector:'edge',style:{'curve-style':'bezier','line-color':'#9ca3af','target-arrow-shape':'triangle','target-arrow-color':'#9ca3af','width':1.4,'opacity':0.9}},
    {selector:'.faded',style:{'opacity':0.12}},
    {selector:'.highlight',style:{'border-width':2,'border-color':'#111827'}}
  ]
});

function log(msg, level="info"){
  const list = document.getElementById("logList");
  const item = document.createElement("div");
  item.className = `log-item ${level === "error" ? "error" : ""}`;
  item.textContent = `${new Date().toLocaleTimeString()} ${msg}`;
  list.appendChild(item);
  if (autoScroll) {
    list.scrollTop = list.scrollHeight;
  }
}

function clearLog(){
  document.getElementById("logList").innerHTML = "";
}

async function api(url, opt={}, retry=true){
  const resp = await fetch(url, {headers:{...getHeaders(), ...(opt.headers||{})}, ...opt});
  const text = await resp.text();
  if(resp.status === 401 && retry){
    const msg = text || "";
    if(msg.includes("Signature has expired") || msg.includes("missing bearer token") || msg.includes("invalid token")){
      try{
        await refreshToken();
        return await api(url, opt, false);
      }catch(e){
        log(`刷新 token 失败: ${e}`, "error");
      }
    }
  }
  if(!resp.ok){
    throw new Error(text || resp.statusText);
  }
  return text ? JSON.parse(text) : {};
}

function setStatusBar(text){
  document.getElementById("statusBar").textContent = text;
}

function actionHintText(action){
  const map = {
    expand: "从当前方向向外扩展相邻子主题。",
    deepen: "在当前方向内部下钻更细问题。",
    pivot: "转向相邻但不同的方向，避免陷入单一路线。",
    converge: "收敛到更具体可执行的核心问题。",
    stop: "结束本轮探索，保留当前成果。"
  };
  return map[action] || "";
}

function updateActionHint(){
  const action = document.getElementById("action").value;
  document.getElementById("actionHint").textContent = actionHintText(action);
}

function taskStatusBadge(status){
  if(status === "done") return '<span class="badge ok">完成</span>';
  if(status === "failed") return '<span class="badge fail">失败</span>';
  if(status === "searching" || status === "planning") return '<span class="badge warn">进行中</span>';
  return `<span class=\"badge\">${status || "unknown"}</span>`;
}

function renderTasks(){
  const div = document.getElementById("tasks");
  if(!taskCache.length){
    div.textContent = "暂无任务";
    return;
  }
  div.innerHTML = taskCache.map(t => {
    const active = t.task_id === currentTask ? "active" : "";
    return `<div class="task-item ${active}" onclick="selectTask('${t.task_id}')">
      <div class="task-title">${escapeHtml(t.topic || t.task_id)} ${taskStatusBadge(t.status)}</div>
      <div class="task-meta">${t.task_id} · rounds=${t.rounds_total || 0} · papers=${t.papers_total || 0} · updated=${(t.updated_at || "").replace("T"," ").slice(0,19)}</div>
    </div>`;
  }).join(" ");
}

function renderDirections(task){
  const div = document.getElementById("directions");
  if(!task || !(task.directions || []).length){
    div.textContent = "暂无方向";
    return;
  }
  div.innerHTML = (task.directions || []).map(d => {
    return `<div class="dir-item" onclick="selectDirection(${d.direction_index})">
      <span class="dir-index">${d.direction_index}</span>
      <span class="dir-name">${escapeHtml(d.name)}</span>
    </div>`;
  }).join(" ");
}

function escapeHtml(text){
  return (text || "").replace(/[&<>\"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
}

async function refreshTasks(){
  try{
    const data = await api("/api/v1/research/tasks?limit=8");
    taskCache = data.items || [];
    renderTasks();
    if(!currentTask && taskCache.length){
      selectTask(taskCache[0].task_id);
    } else {
      const t = taskCache.find(x => x.task_id === currentTask);
      renderDirections(t);
      if(currentTask){
        refreshSavedPapers();
      }
    }
  }catch(e){
    log(String(e), "error");
  }
}

function selectDirection(idx){
  document.getElementById("directionIndex").value = idx;
  log(`选择方向 ${idx}`);
}

async function selectTask(taskId){
  currentTask = taskId;
  currentRound = null;
  const task = taskCache.find(t => t.task_id === taskId);
  renderDirections(task);
  setStatusBar(`当前任务 ${taskId} · ${task ? task.status : ""}`);
  log(`切换任务 ${taskId}`);
  await loadTree();
  await refreshSavedPapers();
}

async function createTask(){
  const topic = document.getElementById("topic").value.trim();
  if(!topic){return;}
  try{
    const data = await api("/api/v1/research/tasks", {method:"POST", headers:{"Content-Type":"application/json",...getHeaders()}, body: JSON.stringify({topic})});
    currentTask = data.task_id;
    log(`创建任务 ${currentTask}`);
    await refreshTasks();
  }catch(e){
    log(String(e), "error");
  }
}

async function startExplore(){
  if(!currentTask){return;}
  const direction_index = parseInt(document.getElementById("directionIndex").value || "0");
  if(!direction_index){log("请输入方向序号", "error"); return;}
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/explore/start`, {method:"POST", headers:{"Content-Type":"application/json",...getHeaders()}, body: JSON.stringify({direction_index})});
    currentRound = data.round_id;
    log(`开始轮次 ${currentRound}`);
    await loadTree();
  }catch(e){
    log(String(e), "error");
  }
}

async function continueByIntent(){
  if(!currentTask || !currentRound){
    log("请先开始轮次", "error");
    return;
  }
  const intent_text = (document.getElementById("feedback").value || "").trim();
  if(!intent_text){
    log("请输入自然语言调研需求", "error");
    return;
  }
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/explore/rounds/${currentRound}/next`, {
      method:"POST",
      headers:{"Content-Type":"application/json",...getHeaders()},
      body: JSON.stringify({intent_text})
    });
    currentRound = data.child_round_id;
    log(`已按自然语言创建子轮次 ${currentRound}`);
    await loadTree();
  }catch(e){
    log(String(e), "error");
  }
}

async function propose(){
  if(!currentTask || !currentRound){log("请先开始轮次", "error");return;}
  const action = document.getElementById("action").value;
  const feedback_text = document.getElementById("feedback").value || "";
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/explore/rounds/${currentRound}/propose`, {
      method:"POST", headers:{"Content-Type":"application/json",...getHeaders()},
      body: JSON.stringify({action, feedback_text, candidate_count:4})
    });
    const div = document.getElementById("candidates");
    div.innerHTML = (data.candidates || []).map(c => {
      return `<div>
        <b>${c.candidate_index}. ${escapeHtml(c.name)}</b><br/>
        <span class="small">${(c.queries || []).join(" | ")}</span><br/>
        <button onclick="selectCandidate(${currentRound},${c.candidate_id})">选择并继续</button>
      </div>`;
    }).join("<hr/>");
    log(`生成候选 ${data.candidates.length} 个`);
  }catch(e){
    log(String(e), "error");
  }
}

async function selectCandidate(roundId, candidateId){
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/explore/rounds/${roundId}/select`, {
      method:"POST", headers:{"Content-Type":"application/json",...getHeaders()},
      body: JSON.stringify({candidate_id: candidateId})
    });
    currentRound = data.child_round_id;
    log(`进入子轮次 ${currentRound}`);
    await loadTree();
  }catch(e){
    log(String(e), "error");
  }
}

async function refreshSavedPapers(){
  if(!currentTask){
    document.getElementById("savedPapers").textContent = "暂无";
    return;
  }
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/papers/saved?limit=200`);
    const items = data.items || [];
    const box = document.getElementById("savedPapers");
    if(!items.length){
      box.textContent = "暂无";
      return;
    }
    box.innerHTML = items.map(x => (
      `<div class="task-item" onclick="openSavedPaper('${escapeHtml(x.paper_id)}')">` +
      `<div class="task-title">${escapeHtml(x.title || x.paper_id)}</div>` +
      `<div class="task-meta">${x.paper_id} · ${x.year || "-"} · ${x.saved_at || "-"}</div>` +
      `</div>`
    )).join("");
  }catch(e){
    log(`加载已保存论文失败: ${e}`, "error");
  }
}

async function openSavedPaper(paperId){
  try{
    if(!currentTask){return;}
    const data = await api(`/api/v1/research/tasks/${currentTask}/papers/${encodeURIComponent(paperId)}`);
    currentPaperToken = data.paper_id || paperId;
    showPaperDetail(data);
  }catch(e){
    log(String(e), "error");
  }
}

function loadPositions(taskId){
  try{
    const raw = localStorage.getItem(`memomate_graph_pos_${taskId}`);
    if(!raw){return {};}
    return JSON.parse(raw) || {};
  }catch(e){
    return {};
  }
}

function savePositions(){
  if(!currentTask){return;}
  const pos = {};
  cy.nodes().forEach(n => {
    pos[n.id()] = n.position();
  });
  localStorage.setItem(`memomate_graph_pos_${currentTask}`, JSON.stringify(pos));
}

function buildElements(nodes, edges){
  const saved = currentTask ? loadPositions(currentTask) : {};
  const parentMap = {};
  edges.forEach(e => {
    if(!parentMap[e.target]){parentMap[e.target] = e.source;}
  });
  const elements = [];
  nodes.forEach(n => {
    const data = {...n};
    if(n.type === "direction" && n.direction_index){
      data.label = `【${n.direction_index}】${n.label}`;
    }
    if(n.type === "round"){
      const actionMap = {expand:"拓展",deepen:"深化",pivot:"转向",converge:"收敛",stop:"停止"};
      const actionLabel = actionMap[n.action] ? `·${actionMap[n.action]}` : "";
      data.label = `第${n.depth || 1}轮${actionLabel}`;
    }
    if(n.type === "paper" && n.label && n.label.length > 120){
      data.label = n.label.slice(0, 120) + "…";
    }
    let position = saved[n.id];
    if(!position){
      const parent = parentMap[n.id];
      const parentPos = saved[parent];
      if(parentPos){
        position = {x: parentPos.x + (Math.random()*80-40), y: parentPos.y + (Math.random()*80-40)};
      }
    }
    elements.push(position ? {data, position} : {data});
  });
  edges.forEach(e => elements.push({data:{source:e.source,target:e.target,type:e.type,weight:e.weight||1}}));
  return elements;
}

function runLayout(force=false){
  const saved = currentTask ? loadPositions(currentTask) : {};
  const hasSaved = Object.keys(saved || {}).length > 0;
  if(layoutLocked && hasSaved && !force){
    cy.layout({name:"preset", fit:true, padding:24}).run();
    return;
  }
  const roots = cy.nodes('[type = "topic"]');
  cy.layout({
    name:"breadthfirst",
    directed:true,
    spacingFactor:1.25,
    roots: roots,
    padding:30,
    avoidOverlap:true,
    nodeDimensionsIncludeLabels:true,
    transform: function(_node, position){
      return {x: position.y, y: position.x};
    }
  }).run();
  cy.once('layoutstop', () => savePositions());
}

function renderGraph(nodes, edges){
  lastRenderNodes = nodes || [];
  lastRenderEdges = edges || [];
  cy.elements().remove();
  cy.add(buildElements(nodes, edges));
  runLayout(false);
}

async function loadTree(){
  if(!currentTask){return;}
  try{
    const limitVal = parseInt(document.getElementById("paperLimit").value || "8");
    paperLimit = Math.max(1, Math.min(50, isNaN(limitVal) ? 8 : limitVal));
    const data = await api(`/api/v1/research/tasks/${currentTask}/graph?view=tree&include_papers=${includePapers}&paper_limit=${paperLimit}`);
    renderGraph(data.nodes || [], data.edges || []);
    const stat = data.stats || {};
    const roundCount = stat.round_count || 0;
    const status = data.status || "";
    setStatusBar(`当前任务 ${currentTask} · 状态 ${status} · 轮次 ${roundCount} · 节点 ${(data.nodes || []).length}`);
  }catch(e){
    log(String(e), "error");
  }
}

function showDetail(node){
  const card = document.getElementById("detailCard");
  const d = node.data();
  if(!d){card.classList.remove("show");return;}
  const typeLabels = {topic:"主题",direction:"方向",round:"轮次",paper:"论文"};
  let html = `<div class="detail-title">${escapeHtml(d.label || d.id)}</div>`;
  html += `<div class="detail-line">类型：${typeLabels[d.type] || d.type}</div>`;
  if(d.type === "paper"){
    currentPaperToken = d.paper_id || d.id;
    card.innerHTML = `<div class="detail-title">${escapeHtml(d.label || d.id)}</div><div class="detail-line">加载论文详情...</div>`;
    card.classList.add("show");
    loadPaperDetail(currentPaperToken);
    return;
  } else {
    if(d.direction_index){
      html += `<div class="detail-block"><span class="label">方向序号</span>${d.direction_index}</div>`;
    }
    if(d.action){
      const actionMap = {expand:"拓展",deepen:"深化",pivot:"转向",converge:"收敛",stop:"停止"};
      html += `<div class="detail-block"><span class="label">动作</span>${actionMap[d.action] || d.action}</div>`;
    }
    if(d.status){
      html += `<div class="detail-block"><span class="label">状态</span>${d.status}</div>`;
    }
    if(d.feedback_text){
      html += `<div class="detail-block"><span class="label">反馈</span>${escapeHtml(d.feedback_text)}</div>`;
    }
  }
  card.innerHTML = html;
  card.classList.add("show");
  window._currentAbstract = d.abstract || "";
}

function showPaperDetail(d){
  const card = document.getElementById("detailCard");
  const authors = (d.authors || []).slice(0, 8).join(", ");
  let html = `<div class="detail-title">${escapeHtml(d.title || d.paper_id || "-")}</div>`;
  html += `<div class="detail-line">论文 ID：${escapeHtml(d.paper_id || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">作者</span>${escapeHtml(authors || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">年份</span>${d.year || "-"}</div>`;
  html += `<div class="detail-block"><span class="label">Venue</span>${escapeHtml(d.venue || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">DOI</span>${escapeHtml(d.doi || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">URL</span>${d.url ? `<a href=\"${d.url}\" target=\"_blank\">打开链接</a>` : "-"}</div>`;
  html += `<div class="detail-block"><span class="label">来源</span>${escapeHtml(d.source || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">全文</span>${escapeHtml(d.fulltext_status || "-")}</div>`;
  html += `<div class="detail-block"><span class="label">保存状态</span>${d.saved ? "已保存" : "未保存"}</div>`;
  html += `<div class="detail-block"><span class="label">要点状态</span>${escapeHtml(d.key_points_status || "none")}</div>`;
  const abs = d.abstract || "";
  const absShort = abs.length > 260 ? abs.slice(0, 260) + "…" : abs;
  html += `<div class="detail-block"><span class="label">摘要</span><span id=\"absText\">${escapeHtml(absShort || "-")}</span>`;
  if(abs.length > 260){
    html += ` <button class=\"ghost\" onclick=\"toggleAbstract()\" style=\"padding:2px 6px;font-size:11px;\">展开</button>`;
  }
  html += `</div>`;
  if(d.method_summary){
    html += `<div class="detail-block"><span class="label">方法总结</span>${escapeHtml(d.method_summary)}</div>`;
  }
  if(d.key_points){
    html += `<div class="detail-block"><span class="label">AI 要点</span>${escapeHtml(d.key_points).replace(/\\n/g, "<br/>")}</div>`;
  }
  html += `<div class="detail-actions">`;
  html += `<button ${d.saved ? "disabled" : ""} onclick="saveCurrentPaper()">保存</button>`;
  html += `<button class="ghost" onclick="summarizeCurrentPaper()">AI 总结要点</button>`;
  html += `</div>`;
  card.innerHTML = html;
  card.classList.add("show");
  window._currentAbstract = d.abstract || "";
}

async function loadPaperDetail(paperId){
  if(!currentTask || !paperId){return;}
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/papers/${encodeURIComponent(paperId)}`);
    currentPaperToken = data.paper_id || paperId;
    showPaperDetail(data);
  }catch(e){
    log(String(e), "error");
  }
}

async function saveCurrentPaper(){
  if(!currentTask || !currentPaperToken){return;}
  try{
    const data = await api(`/api/v1/research/tasks/${currentTask}/papers/${encodeURIComponent(currentPaperToken)}/save`, {
      method: "POST",
      headers: {"Content-Type":"application/json",...getHeaders()},
      body: JSON.stringify({})
    });
    log(`已保存: ${data.saved_path}`);
    await refreshSavedPapers();
    await loadPaperDetail(currentPaperToken);
  }catch(e){
    log(String(e), "error");
  }
}

async function summarizeCurrentPaper(){
  if(!currentTask || !currentPaperToken){return;}
  try{
    await api(`/api/v1/research/tasks/${currentTask}/papers/${encodeURIComponent(currentPaperToken)}/summarize`, {
      method:"POST",
      headers: {...getHeaders()}
    });
    log("已触发 AI 要点总结");
  }catch(e){
    log(String(e), "error");
  }
}

function toggleAbstract(){
  const abs = window._currentAbstract || "";
  const el = document.getElementById("absText");
  if(!el){return;}
  const isShort = el.textContent.length < abs.length;
  if(isShort){
    el.textContent = abs;
  }else{
    el.textContent = abs.slice(0, 260) + "…";
  }
}

cy.on('tap', 'node', (evt) => {
  const node = evt.target;
  const data = node.data();
  cy.elements().removeClass('faded');
  let keep = node.closedNeighborhood();
  if (data.type === "round" || data.type === "paper") {
    const dirNode = cy.nodes().filter(n => n.data('type') === 'direction' && n.data('direction_index') === data.direction_index);
    const topicNode = cy.nodes().filter(n => n.data('type') === 'topic');
    keep = keep.union(dirNode).union(topicNode);
  }
  cy.elements().not(keep).addClass('faded');
  showDetail(node);
  if(data.direction_index){
    document.getElementById("directionIndex").value = data.direction_index;
  }
});

cy.on('tap', (evt) => {
  if(evt.target === cy){
    cy.elements().removeClass('faded');
    document.getElementById("detailCard").classList.remove("show");
  }
});

cy.on('dragfree', 'node', () => savePositions());

function reflowLayout(){
  runLayout(true);
}

cy.on('viewport', () => {
  if (layoutLocked) {
    savePositions();
  }
});

const splitter = document.getElementById('splitter');
let drag = false;
let startY = 0;
let startH = 0;

splitter.addEventListener('mousedown', (e) => {
  drag = true; startY = e.clientY; startH = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--log-h'));
  document.body.style.cursor = 'row-resize';
});
window.addEventListener('mousemove', (e) => {
  if(!drag){return;}
  const delta = startY - e.clientY;
  const next = Math.max(120, Math.min(400, startH + delta));
  document.documentElement.style.setProperty('--log-h', `${next}px`);
});
window.addEventListener('mouseup', () => {
  if(drag){drag=false; document.body.style.cursor = 'default';}
});

function bindToggles(){
  const paperToggle = document.getElementById('togglePapers');
  const paperLimitInput = document.getElementById('paperLimit');
  const lockToggle = document.getElementById('toggleLock');
  const refreshToggle = document.getElementById('autoRefreshGraph');
  const autoTaskToggle = document.getElementById('autoRefreshTasks');
  const autoScrollToggle = document.getElementById('autoScroll');
  const actionSelect = document.getElementById('action');

  paperToggle.addEventListener('change', () => {
    includePapers = paperToggle.checked;
    loadTree();
  });
  paperLimitInput.addEventListener('change', () => {
    loadTree();
  });
  lockToggle.addEventListener('change', () => {
    layoutLocked = lockToggle.checked;
  });
  refreshToggle.addEventListener('change', () => {
    autoRefreshGraph = refreshToggle.checked;
  });
  autoTaskToggle.addEventListener('change', () => {
    autoRefreshTasks = autoTaskToggle.checked;
  });
  autoScrollToggle.addEventListener('change', () => {
    autoScroll = autoScrollToggle.checked;
  });
  actionSelect.addEventListener('change', () => {
    updateActionHint();
  });
}

bindToggles();
updateActionHint();
setCurrentUserUI();
refreshToken(currentUserId)
  .then(() => loadDevUsers())
  .then(() => refreshTasks())
  .catch(e => log(String(e), "error"));
setInterval(() => {
  if(autoRefreshTasks){
    refreshTasks();
  }
  if(autoRefreshGraph && currentTask){
    loadTree();
  }
}, 10000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
