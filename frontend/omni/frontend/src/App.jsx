import {useEffect, useMemo, useState} from "react";
import {runOmniSightScan} from "./services/api.js";

const SAMPLE_HISTORY=[
 {id:"1041",url:"https://shoplite.local",status:"completed",findings:3,fixed:3},
 {id:"1040",url:"https://staging.acme.io/dashboard",status:"failed",findings:2,fixed:0},
 {id:"1039",url:"https://acme.io/marketing",status:"completed",findings:4,fixed:4}
];
const STAGES=["Detect","Reason","Fix","Validate","Verify & Ship"];
const PATCH=`.checkout .cta-button {
-   min-width: 320px;
+   width: 100%;
+   max-width: 100%;
    padding: 14px 24px;
}`;
const escText=v=>String(v??"");
function normalizeFinding(record,i){
 const f=record.finding||record, r=f.region;
 return {id:f.finding_id||f.id||`vlm-${i+1}`,title:f.observation||f.title||String(f.category||"Visual finding").replaceAll("_"," "),
 severity:f.severity||"low",confidence:f.confidence==null?"—":`${Math.round(Number(f.confidence)*100)}%`,
 selector:f.target_candidate||f.selector||"(visual region)",cause:(f.potential_causes?.length?f.potential_causes.join("; "):f.evidence||f.description||f.observation||"VLM visual finding"),
 box:r?[r.x,r.y,r.width,r.height]:[20,20+i*48,120,40]};
}
function toast(msg){const root=document.getElementById("toast-root");if(!root)return;const e=document.createElement("div");e.className="toast";e.textContent="✨ "+msg;root.appendChild(e);setTimeout(()=>e.remove(),3500)}
function Tag({kind}){return <span className={`tag tag--${kind||"box"}`}>{kind?kind.toUpperCase():"SANDBOX"}</span>}
function Sidebar({route,setRoute,history,startScan}){
 const [q,setQ]=useState(""); const recent=history.filter(x=>x.url.toLowerCase().includes(q.toLowerCase())).slice(0,5);
 return <aside className="sidebar">
  <button className="sidebar__brand" onClick={()=>setRoute("landing")}><div className="sidebar__logo">◉</div><div className="sidebar__brand-name">OmniSight</div></button>
  <button className="btn-new-chat" onClick={()=>setRoute("landing")}>＋ New Scan</button>
  <div className="sidebar__search-wrap"><span className="sidebar__search-icon">⌕</span><input className="sidebar__search" placeholder="Search scans...  ( / )" value={q} onChange={e=>setQ(e.target.value)}/></div>
  <div className="nav-group">
   {["landing","scan","history","models"].map((r,i)=><button key={r} className={`nav-item ${route===r?"nav-item--active":""}`} onClick={()=>r!=="scan"||history.length?setRoute(r):null}>{["▦ Overview","◉ Active Scan","⌁ History","⚙ Models"][i]}</button>)}
  </div>
  <div className="sidebar__section-title">Recent Scans</div>
  <div className="recent-scans-list">{recent.map(h=><button key={h.id} className="recent-scan-btn truncate" onClick={()=>startScan(h.url)}>○ <span className="truncate">{h.url.replace(/^https?:\/\//,"")}</span></button>)}</div>
  <div className="sidebar__footer"><div className="sidebar__profile-pic">OS</div><div><div className="sidebar__user-name">OmniSight Engine</div><div className="sidebar__user-sub">Autonomous UI Bot</div></div></div>
 </aside>
}
function Landing({startScan,deep,setDeep}){
 const [url,setUrl]=useState("https://shoplite.local");
 return <div className="landing-view">
  <div className="ai-orb-wrap"><div className="ai-orb"/></div>
  <div className="landing-greeting">Hello, Developer</div>
  <h1 className="landing-heading">What should we inspect today?</h1>
  <div className="stat-row"><span><i/>Playwright captures the page</span><span><i/>A vision model finds defects</span><span><i/>A language model writes fixes</span></div>
  <div className="scan-composer">
   <input className="scan-composer__input" value={url} onChange={e=>setUrl(e.target.value)} onKeyDown={e=>e.key==="Enter"&&startScan(url)} placeholder="Enter website URL to inspect..."/>
   <div className="scan-composer__bottom"><div className="scan-composer__pills">
    <button className="deep-scan-btn" aria-pressed={deep} onClick={()=>setDeep(!deep)}>◉ Deep scan</button>
    <select className="model-select" defaultValue="Backend VLM Adapter"><option>Backend VLM Adapter</option><option>OmniVision-VL 7B</option></select>
    <select className="model-select" defaultValue="OmniCoder-LLM 70B"><option>OmniCoder-LLM 70B</option><option>OmniCoder-LLM 8B</option></select>
   </div><button className="btn-scan-submit" onClick={()=>startScan(url)} aria-label="Start scan">↑</button></div>
  </div>
  <div className="quick-cards">
   <div className="q-card" onClick={()=>startScan("https://shoplite.local")}><div className="q-card__icon">▤</div><div className="q-card__title">Capture with Playwright</div><div className="q-card__desc">Playwright opens the target URL and captures full-page visual evidence automatically.</div></div>
   <div className="q-card" onClick={()=>startScan("https://shoplite.local")}><div className="q-card__icon">◉</div><div className="q-card__title">Detect with VLM</div><div className="q-card__desc">A vision model reads the captured page and detects clipped, overlapping and low-contrast elements.</div></div>
   <div className="q-card" onClick={()=>startScan("https://shoplite.local")}><div className="q-card__icon">↗</div><div className="q-card__title">Reason and fix with LLM</div><div className="q-card__desc">A language model reasons about findings and prepares a minimal patch for review.</div></div>
  </div>
 </div>
}
function Pipeline({scan}){return <div className="pipeline-card" style={{"--p":`${Math.min(100,(scan.stage+1)/STAGES.length*100)}%`}}>{STAGES.map((s,i)=><span key={s} style={{display:"contents"}}>{i>0&&<div className="pipe-divider"/>}<div className={`pipe-step ${i<scan.stage?"pipe-step--completed":i===scan.stage?"pipe-step--running":""}`}><div className="pipe-step__row"><div className="pipe-step__badge">{i<scan.stage?"✓":i+1}</div><span>{s}</span></div><Tag kind={i===0||i===4?"vlm":i===1||i===2?"llm":null}/></div></span>)}</div>}
function Dashboard({scan}){
 const [sev,setSev]=useState("all"), [activeId,setActiveId]=useState(null);
 const shown=scan.findings.filter(f=>sev==="all"||f.severity===sev); const active=scan.findings.find(f=>f.id===activeId)||shown[0]||scan.findings[0];
 return <div className="dashboard-view">
  <Pipeline scan={scan}/>
  {scan.status==="running"&&<div className="scan-progress"><span className="scan-progress__spinner"/><span>{scan.progress}</span></div>}
  {scan.error&&<div className="error-note"><strong>Scan failed.</strong> {scan.error}</div>}
  <div className="dash-split"><div className="dash-col">
   <div className="panel score-card"><div className="score-ring"><b>{scan.status==="completed"?100:"–"}</b></div><div><div className="model-row__name">UI health score</div><div className="panel__sub">VLM-powered visual inspection</div></div></div>
   <div className="panel"><div className="panel__head"><div className="panel__title">Findings ({scan.findings.length})</div><span className={`badge badge--${scan.status==="completed"?"success":scan.status==="failed"?"error":"info"}`}>{scan.status}</span></div>
    <div className="panel__body"><div className="chips">{["all","high","medium","low"].map(v=><button key={v} className={`chip ${sev===v?"chip--on":""}`} onClick={()=>setSev(v)}>{v}</button>)}</div>
    {shown.length?shown.map(f=><div key={f.id} className={`finding-item sev-${f.severity} ${active?.id===f.id?"finding-item--selected":""}`} onClick={()=>setActiveId(f.id)}><div><span className={`badge badge--${f.severity}`}>{f.severity}</span> <span className="muted mono">{f.confidence}</span></div><div className="finding-item__title">{f.title}</div><div className="finding-item__meta mono">{f.selector}</div></div>):<div className="empty">No findings yet. The VLM is still inspecting.</div>}</div>
   </div>
  </div><div className="dash-col">
   <div className="panel"><div className="panel__head"><div className="panel__title"><Tag kind="vlm"/> Visual perception</div><span className="panel__sub">Playwright evidence</span></div><div className="panel__body"><div className="vis-frame">{scan.status==="running"&&<div className="scan-line"/>}<div style={{minHeight:220,display:"grid",placeItems:"center",padding:20,textAlign:"center"}}><div><b>{scan.capture?.page_title||"Captured webpage"}</b><div className="muted" style={{marginTop:8}}>{scan.capture?.final_url||scan.url}</div><div className="panel__sub" style={{marginTop:8}}>{scan.capture?.screenshot_path||"Waiting for Playwright screenshot…"}</div></div></div></div></div></div>
   <div className="panel"><div className="panel__head"><div className="panel__title"><Tag kind="llm"/> Reasoning</div><span className="panel__sub">{active?.selector||"(waiting)"}</span></div><div className="panel__body">{active?<div className="cause"><strong>Root cause.</strong> {active.cause}</div>:<div className="empty">The LLM reasoning stage starts after VLM findings are available.</div>}</div></div>
   <div className="panel"><div className="panel__head"><div className="panel__title"><Tag kind="llm"/> Generated patch</div><button className="btn" onClick={()=>navigator.clipboard?.writeText(PATCH).then(()=>toast("Patch copied"))}>Copy</button></div><div className="panel__body" style={{padding:0}}><div className="diff-container">{PATCH.split("\n").map((l,i)=><div key={i} className={`diff-line ${l[0]==="+"?"diff-line--add":l[0]==="-"?"diff-line--del":""}`}>{l}</div>)}</div></div></div>
  </div></div>
 </div>
}
function History({history,startScan}){return <div className="models-view"><div className="panel"><div className="panel__head"><div className="panel__title">Scan history</div><span className="panel__sub">{history.length} scans</span></div><div className="panel__body">{history.map(x=><div className="hist-row" key={x.id}><div><div className="model-row__name">{x.url}</div><div className="panel__sub">#{x.id} · {x.findings} findings</div></div><span className={`badge badge--${x.status==="completed"?"success":"error"}`}>{x.status}</span><button className="btn" onClick={()=>startScan(x.url)}>Re-run</button></div>)}</div></div></div>}
function Models(){return <div className="models-view"><div className="panel"><div className="panel__head"><div className="panel__title">Model settings</div></div><div className="panel__body"><div className="field"><label>Backend API base URL</label><input value={import.meta.env.VITE_API_BASE_URL||"http://127.0.0.1:8000"} readOnly/></div><div className="field-grid"><div className="field"><label>VLM</label><input value="Backend VLM Adapter" readOnly/></div><div className="field"><label>LLM</label><input value="OmniCoder-LLM 70B" readOnly/></div></div></div></div></div>}
export default function App(){
 const [route,setRoute]=useState("landing"),[deep,setDeep]=useState(true),[scan,setScan]=useState(null);
 const [history,setHistory]=useState(()=>{try{return JSON.parse(localStorage.getItem("omnisight.history.react"))||SAMPLE_HISTORY}catch{return SAMPLE_HISTORY}});
 const [theme,setTheme]=useState(()=>localStorage.getItem("omnisight.theme.react")||"light");
 useEffect(()=>{document.documentElement.dataset.theme=theme;localStorage.setItem("omnisight.theme.react",theme)},[theme]);
 useEffect(()=>localStorage.setItem("omnisight.history.react",JSON.stringify(history)),[history]);
 async function startScan(raw){
  let url=raw.trim(); if(!/^https?:\/\//i.test(url))url="https://"+url;
  try{new URL(url)}catch{return toast("Enter a valid URL")}
  const s={id:String(Date.now()),url,status:"running",stage:0,progress:"Preparing scan…",findings:[],capture:null,error:null};
  setScan(s);setRoute("scan");
  try{
   const out=await runOmniSightScan(url,deep,msg=>setScan(v=>({...v,progress:msg})));
   const findings=(out.result.findings||[]).map(normalizeFinding);
   setScan(v=>({...v,stage:4,status:"completed",progress:"Scan complete",findings,capture:out.capture,backendScanId:out.scanId}));
   setHistory(h=>[{id:s.id,url,status:"completed",findings:findings.length,fixed:0},...h].slice(0,50));toast(`Scan complete: ${findings.length} findings`);
  }catch(e){setScan(v=>({...v,status:"failed",error:e.message}));setHistory(h=>[{id:s.id,url,status:"failed",findings:0,fixed:0},...h].slice(0,50));toast("Scan failed: "+e.message)}
 }
 const crumb=route==="landing"?"Overview":route==="scan"?(scan?.url||"Active Scan"):route[0].toUpperCase()+route.slice(1);
 return <div className="app-shell"><Sidebar route={route} setRoute={setRoute} history={history} startScan={startScan}/><div className="workspace"><header className="topbar"><div className="topbar__title"><span>OmniSight</span><span className="muted">/</span><span style={{fontWeight:400}}>{crumb}</span></div><div className="topbar__actions"><button className="btn" onClick={()=>setTheme(theme==="dark"?"light":"dark")}>{theme==="dark"?"☀ Light":"☾ Dark"}</button></div></header><div className="main-content">{route==="landing"?<Landing startScan={startScan} deep={deep} setDeep={setDeep}/>:route==="history"?<History history={history} startScan={startScan}/>:route==="models"?<Models/>:scan?<Dashboard scan={scan}/>:<Landing startScan={startScan} deep={deep} setDeep={setDeep}/>}</div></div></div>
}
