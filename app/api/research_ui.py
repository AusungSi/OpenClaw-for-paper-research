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
    :root{--bg:#f4f1ea;--ink:#1e1c19;--muted:#6f695f;--card:#fffdf8;--line:#ded6c8;--accent:#0f766e;}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:"Source Han Sans SC","Noto Sans SC",sans-serif}
    .layout{display:grid;grid-template-columns:360px 1fr;min-height:100vh}
    .left{background:var(--card);border-right:1px solid var(--line);padding:14px;overflow:auto}
    .right{display:grid;grid-template-rows:1fr 220px}
    .box{margin-bottom:14px;padding:10px;border:1px solid var(--line);border-radius:10px;background:#fff}
    .row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
    .row>input,.row>select,.row>textarea{flex:1;padding:8px;border:1px solid #d6cfc2;border-radius:8px}
    textarea{min-height:72px;resize:vertical}
    button{padding:7px 10px;border:0;background:var(--accent);color:#fff;border-radius:8px;cursor:pointer}
    button.alt{background:#6b7280}
    #cy{min-height:0}
    .log{font-size:12px;line-height:1.5;color:var(--muted);padding:10px;overflow:auto;border-top:1px solid var(--line)}
    .small{font-size:12px;color:var(--muted)}
  </style>
</head>
<body>
  <div class="layout">
    <div class="left">
      <div class="box">
        <div class="row"><strong>新建调研任务</strong></div>
        <div class="row"><input id="topic" placeholder="输入主题，例如 ultrasound report generation"/></div>
        <div class="row"><button onclick="createTask()">创建并规划</button></div>
      </div>

      <div class="box">
        <div class="row"><strong>任务与方向</strong></div>
        <div class="row"><button class="alt" onclick="refreshTasks()">刷新任务</button></div>
        <div id="tasks" class="small">加载中...</div>
      </div>

      <div class="box">
        <div class="row"><strong>轮次探索</strong></div>
        <div class="row"><input id="directionIndex" placeholder="方向序号，如 1"/><button onclick="startExplore()">开始</button></div>
        <div class="row"><select id="action"><option>expand</option><option>deepen</option><option>pivot</option><option>converge</option><option>stop</option></select></div>
        <div class="row"><textarea id="feedback" placeholder="输入你的反馈，如：请更关注 hallucination 评估"></textarea></div>
        <div class="row"><button onclick="propose()">生成候选</button></div>
        <div id="candidates" class="small"></div>
      </div>
    </div>
    <div class="right">
      <div id="cy"></div>
      <div class="log" id="log"></div>
    </div>
  </div>
<script>
let currentTask = "";
let currentRound = null;
const token = localStorage.getItem("memomate_access_token") || "";
const headers = token ? {Authorization:`Bearer ${token}`} : {};
let cy = cytoscape({
  container: document.getElementById("cy"),
  elements: [],
  layout: {name:"cose", animate:false, fit:true, padding:30},
  style: [
    {selector:'node',style:{'label':'data(label)','font-size':11,'text-wrap':'wrap','text-max-width':120,'background-color':'#8b8b8b','color':'#222','width':20,'height':20}},
    {selector:'node[type="topic"]',style:{'background-color':'#0f766e','color':'#fff','width':38,'height':38}},
    {selector:'node[type="direction"]',style:{'background-color':'#1d4ed8','color':'#fff','width':30,'height':30}},
    {selector:'node[type="round"]',style:{'background-color':'#6d28d9','color':'#fff','width':26,'height':26}},
    {selector:'node[type="paper"]',style:{'background-color':'#d97706'}},
    {selector:'edge',style:{'curve-style':'bezier','line-color':'#8e877e','target-arrow-shape':'triangle','target-arrow-color':'#8e877e','width':1.4}}
  ]
});
function log(msg){const el=document.getElementById("log");el.textContent=`${new Date().toLocaleTimeString()} ${msg}\\n`+el.textContent;}
async function api(url, opt={}){
  const resp = await fetch(url, {headers:{...headers, ...(opt.headers||{})}, ...opt});
  if(!resp.ok){throw new Error(await resp.text());}
  return await resp.json();
}
async function createTask(){
  const topic=document.getElementById("topic").value.trim();
  if(!topic){return;}
  const data=await api("/api/v1/research/tasks",{method:"POST",headers:{"Content-Type":"application/json",...headers},body:JSON.stringify({topic})});
  currentTask=data.task_id;log(`创建任务 ${currentTask}`);refreshTasks();
}
async function refreshTasks(){
  const data=await api("/api/v1/research/tasks?limit=8");
  const div=document.getElementById("tasks");
  if(!data.items.length){div.textContent="暂无任务";return;}
  div.innerHTML=data.items.map(t=>`<div><a href="#" onclick="selectTask('${t.task_id}')">${t.task_id}</a> | ${t.status} | rounds=${t.rounds_total||0}</div>`).join("");
  if(!currentTask){selectTask(data.items[0].task_id);}
}
async function selectTask(taskId){
  currentTask=taskId;log(`切换任务 ${taskId}`);await loadTree();
}
async function startExplore(){
  if(!currentTask){return;}
  const direction_index=parseInt(document.getElementById("directionIndex").value||"0");
  const data=await api(`/api/v1/research/tasks/${currentTask}/explore/start`,{
    method:"POST",headers:{"Content-Type":"application/json",...headers},body:JSON.stringify({direction_index})
  });
  currentRound=data.round_id;log(`开始轮次 ${currentRound}`);await loadTree();
}
async function propose(){
  if(!currentTask || !currentRound){return;}
  const action=document.getElementById("action").value;
  const feedback_text=document.getElementById("feedback").value||"";
  const data=await api(`/api/v1/research/tasks/${currentTask}/explore/rounds/${currentRound}/propose`,{
    method:"POST",headers:{"Content-Type":"application/json",...headers},body:JSON.stringify({action,feedback_text,candidate_count:4})
  });
  const div=document.getElementById("candidates");
  div.innerHTML=(data.candidates||[]).map(c=>`<div><b>${c.candidate_index}. ${c.name}</b><br/>${(c.queries||[]).join(" | ")}<br/><button onclick="selectCandidate(${currentRound},${c.candidate_id})">选择并继续</button></div>`).join("<hr/>");
  log(`生成候选 ${data.candidates.length} 个`);
}
async function selectCandidate(roundId,candidateId){
  const data=await api(`/api/v1/research/tasks/${currentTask}/explore/rounds/${roundId}/select`,{
    method:"POST",headers:{"Content-Type":"application/json",...headers},body:JSON.stringify({candidate_id:candidateId})
  });
  currentRound=data.child_round_id;log(`进入子轮次 ${currentRound}`);await loadTree();
}
async function loadTree(){
  if(!currentTask){return;}
  const data=await api(`/api/v1/research/tasks/${currentTask}/explore/tree`);
  renderGraph(data.nodes||[], data.edges||[]);
}
function renderGraph(nodes,edges){
  const elements=[];
  nodes.forEach(n=>elements.push({data:{id:n.id,label:n.label,type:n.type}}));
  edges.forEach(e=>elements.push({data:{source:e.source,target:e.target,type:e.type}}));
  cy.elements().remove();
  cy.add(elements);
  cy.layout({name:"cose",animate:false,fit:true,padding:24}).run();
}
refreshTasks().catch(e=>log(String(e)));
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
