const SERVICES = {
  smartscraper:{name:"SmartScraper",tag:"URL + prompt → JSON",desc:"Extract structured data from any page by describing what you want.",endpoint:"/v1/smartscraper",fields:[
    {k:"website_url",label:"Website URL",type:"text",ph:"https://example.com/products"},
    {k:"user_prompt",label:"What do you want?",type:"prompt",ph:"Extract every product with name, price and link"},
    {k:"output_schema",label:"Output schema",hint:"optional JSON Schema",type:"json"},
    {row:[{k:"render_heavy_js",label:"Render JavaScript",type:"select",opts:["auto","true","false"]},
          {k:"number_of_scrolls",label:"Scrolls",type:"number",def:0},
          {k:"total_pages",label:"Pages (follow Next)",type:"number",def:1}]},
    {k:"main_content_only",label:"Main content only (drop menus/footers)",type:"check"},
    {k:"website_html",label:"…or paste HTML instead of a URL",hint:"optional",type:"code"},
  ]},
  searchscraper:{name:"SearchScraper",tag:"Search the web → JSON",desc:"Searches the web, reads the top results and answers with sources.",endpoint:"/v1/searchscraper",fields:[
    {k:"user_prompt",label:"Question",type:"prompt",ph:"What are the current prices of the iPhone 17 models?"},
    {k:"output_schema",label:"Output schema",hint:"optional",type:"json"},
    {row:[{k:"num_results",label:"Websites to read",type:"number",def:3},{k:"render_heavy_js",label:"Render JavaScript",type:"select",opts:["auto","true","false"]}]},
    {k:"extraction_mode",label:"AI extraction (off = return Markdown of each page)",type:"check",def:true},
  ]},
  crawl:{name:"SmartCrawler",tag:"Whole site → JSON",desc:"Follows links across a site, then extracts one combined answer. Runs in the background.",endpoint:"/v1/crawl",async:true,fields:[
    {k:"url",label:"Start URL",type:"text",ph:"https://docs.example.com"},
    {k:"prompt",label:"What do you want?",type:"prompt",ph:"List every pricing plan with features"},
    {k:"schema",label:"Output schema",hint:"optional",type:"json"},
    {row:[{k:"depth",label:"Depth",type:"number",def:2},{k:"max_pages",label:"Max pages",type:"number",def:10},{k:"render_heavy_js",label:"Render JavaScript",type:"select",opts:["auto","true","false"]}]},
    {k:"include_paths",label:"Only these paths",hint:"comma separated, e.g. /blog/*",type:"list"},
    {k:"exclude_paths",label:"Skip these paths",hint:"comma separated",type:"list"},
    {k:"extraction_mode",label:"AI extraction (off = Markdown only)",type:"check",def:true},
    {k:"sitemap",label:"Also use sitemap.xml",type:"check"},
    {k:"same_domain_only",label:"Stay on the same domain",type:"check",def:true},
    {k:"respect_robots",label:"Respect robots.txt",type:"check",def:true},
  ]},
  agentic:{name:"AgenticScraper",tag:"Browser steps → JSON",desc:"Describe clicks/typing in plain English; Claude drives a real browser, then extracts.",endpoint:"/v1/agentic-scrapper",fields:[
    {k:"url",label:"Start URL",type:"text",ph:"https://example.com/login"},
    {k:"steps",label:"Steps",hint:"one per line",type:"lines",ph:"Type 'laptop' in the search box and press Enter\nClick the first result"},
    {k:"user_prompt",label:"What to extract at the end",hint:"optional",type:"prompt",ph:"Product name, price and specs"},
    {k:"output_schema",label:"Output schema",hint:"optional",type:"json"},
    {k:"ai_extraction",label:"AI extraction (off = return Markdown)",type:"check",def:true},
    {k:"allow_other_domains",label:"Allow navigation to other domains",type:"check"},
  ]},
  markdownify:{name:"Markdownify",tag:"URL → Markdown",desc:"Clean Markdown of any page. No AI call, instant.",endpoint:"/v1/markdownify",fields:[
    {k:"website_url",label:"Website URL",type:"text",ph:"https://example.com/article"},
    {row:[{k:"render_heavy_js",label:"Render JavaScript",type:"select",opts:["auto","true","false"]},{k:"number_of_scrolls",label:"Scrolls",type:"number",def:0}]},
    {k:"main_content_only",label:"Main content only",type:"check"},
  ]},
  scrape:{name:"Scrape",tag:"URL → HTML",desc:"Raw HTML (optionally rendered in a browser). No AI call.",endpoint:"/v1/scrape",fields:[
    {k:"website_url",label:"Website URL",type:"text",ph:"https://example.com"},
    {row:[{k:"render_heavy_js",label:"Render JavaScript",type:"select",opts:["auto","true","false"]},{k:"number_of_scrolls",label:"Scrolls",type:"number",def:0}]},
  ]},
  sitemap:{name:"Sitemap",tag:"Site → URL list",desc:"Every URL listed in the site's sitemap(s).",endpoint:"/v1/sitemap",fields:[
    {k:"website_url",label:"Website URL",type:"text",ph:"https://example.com"},
  ]},
};
let current = "smartscraper", lastResp = null;
const $ = id => document.getElementById(id);
try { localStorage.removeItem("sc_key"); } catch {}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function fieldHTML(f){
  if (f.row) return `<div class="row">${f.row.map(x=>`<div>${fieldHTML(x)}</div>`).join("")}</div>`;
  const hint = f.hint ? ` <span class="hint">(${f.hint})</span>` : "";
  if (f.type==="check") return `<label class="check"><input type="checkbox" name="${f.k}" ${f.def?"checked":""}> ${f.label}</label>`;
  let input;
  if (f.type==="prompt") input = `<textarea class="prompt" name="${f.k}" placeholder="${esc(f.ph||"")}"></textarea>`;
  else if (["json","code","lines"].includes(f.type)) input = `<textarea name="${f.k}" placeholder="${esc(f.ph|| (f.type==="json" ? '{"type":"object","properties":{"title":{"type":"string"}}}' : ""))}"></textarea>`;
  else if (f.type==="select") input = `<select name="${f.k}">${f.opts.map(o=>`<option>${o}</option>`).join("")}</select>`;
  else input = `<input type="${f.type==="number"?"number":"text"}" name="${f.k}" value="${f.def ?? ""}" placeholder="${esc(f.ph||"")}">`;
  return `<label>${f.label}${hint}</label>${input}`;
}
function flat(fields){ return fields.flatMap(f => f.row ? f.row : [f]); }

function select(name){
  current = name; const s = SERVICES[name];
  $("title").textContent = s.name; $("desc").textContent = s.desc;
  $("form").innerHTML = s.fields.map(fieldHTML).join("") +
    `<div class="actions"><button class="btn primary" id="go">Run</button><button type="button" class="btn" id="copyCurl">Copy cURL</button></div><div id="formErr" class="err"></div>`;
  $("copyCurl").addEventListener("click", copyCurl);
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.s===name));
}

function body(){
  const fd = $("form"), out = {};
  for (const f of flat(SERVICES[current].fields)) {
    const el = fd.elements[f.k]; if (!el) continue;
    if (f.type==="check") { out[f.k] = el.checked; continue; }
    const v = el.value.trim(); if (v==="") continue;
    if (f.type==="json") out[f.k] = JSON.parse(v);
    else if (f.type==="number") out[f.k] = Number(v);
    else if (f.type==="list") out[f.k] = v.split(",").map(x=>x.trim()).filter(Boolean);
    else if (f.type==="lines") out[f.k] = v.split("\n").map(x=>x.trim()).filter(Boolean);
    else if (f.type==="select") out[f.k] = v==="auto" ? "auto" : v==="true";
    else out[f.k] = v;
  }
  return out;
}

async function api(path, opts={}){
  const r = await fetch(path, {...opts, credentials:"same-origin", headers:{"Content-Type":"application/json","X-Requested-With":"fetch",...(opts.headers||{})}});
  const j = await r.json().catch(()=>({error:r.statusText}));
  if (r.status === 401 && !path.startsWith("/auth/")) showLogin();
  if (!r.ok) throw Object.assign(new Error(j.error ? (typeof j.error==="string"?j.error:JSON.stringify(j.error)) : JSON.stringify(j.detail||j)), {resp:j});
  return j;
}

async function run(e){
  e.preventDefault(); $("formErr").textContent = "";
  let b; try { b = body(); } catch(err){ $("formErr").textContent = "Schema is not valid JSON: " + err.message; return; }
  const btn = $("go"); btn.disabled = true; btn.textContent = "Running…";
  const t0 = Date.now();
  try {
    let j = await api(SERVICES[current].endpoint, {method:"POST", body:JSON.stringify(b)});
    if (SERVICES[current].async) {
      show({...j}, t0); loadHistory();
      while (j.status === "processing") {
        await new Promise(r=>setTimeout(r,2000));
        j = await api(`/v1/crawl/${j.request_id}`);
        btn.textContent = `Crawling… ${j.result?.progress?.pages_crawled ?? 0} pages`;
        show(j, t0);
      }
    }
    show(j, t0);
  } catch(err){ show(err.resp || {status:"failed", error:err.message}, t0); }
  finally { btn.disabled = false; btn.textContent = "Run"; loadHistory(); }
}

function show(j, t0){
  lastResp = j; $("out").hidden = false;
  const u = j.usage || {};
  $("outMeta").innerHTML = [
    `<span class="badge ${esc(j.status)}">${esc(j.status||"")}</span>`,
    j.request_id ? `<span>id ${esc(j.request_id.slice(0,8))}</span>` : "",
    t0 ? `<span>${((Date.now()-t0)/1000).toFixed(1)}s</span>` : (j.duration_s ? `<span>${j.duration_s}s</span>`:""),
    u.llm_calls ? `<span>${u.llm_calls} Claude call(s) · ${u.input_tokens} in / ${u.output_tokens} out tokens</span>` : "",
    u.cost_usd_estimate ? `<span>~$${u.cost_usd_estimate.toFixed(4)}${u.provider==="claude_cli"?" (subscription, not billed)":""}</span>` : "",
  ].join("");
  const views = {"Result": j.result ?? j.llm_result ?? j.html ?? j.urls ?? j.error ?? j};
  if (j.result?.llm_result !== undefined) views["Result"] = j.result.llm_result;
  if (j.result?.pages || j.pages) views["Pages"] = j.result?.pages || j.pages;
  if (j.result?.steps_log || j.steps_log) views["Steps"] = j.result?.steps_log || j.steps_log;
  views["Full response"] = j;
  $("outTabs").innerHTML = Object.keys(views).map((k,i)=>`<button class="${i?"":"active"}" data-k="${k}">${k}</button>`).join("");
  const render = k => { const v = views[k]; $("outBody").textContent = typeof v === "string" ? v : JSON.stringify(v, null, 2); };
  $("outTabs").querySelectorAll("button").forEach(b => b.onclick = () => { $("outTabs").querySelectorAll("button").forEach(x=>x.classList.remove("active")); b.classList.add("active"); render(b.dataset.k); });
  render("Result");
}

function copyCurl(){
  let b; try { b = body(); } catch { b = {}; }
  const cmd = `curl -X POST ${location.origin}${SERVICES[current].endpoint} \\\n  -H "Content-Type: application/json" \\\n  -H "X-API-Key: $SC_API_KEY" \\\n  -d '${JSON.stringify(b).replace(/'/g,"'\\''")}'`;
  navigator.clipboard?.writeText(cmd); $("formErr").style.color="var(--muted)"; $("formErr").textContent = "cURL copied."; setTimeout(()=>{$("formErr").textContent="";$("formErr").style.color="";},2000);
}

async function loadHistory(){
  try {
    const j = await api("/v1/requests?limit=40");
    $("hist").innerHTML = j.requests.map(r => {
      const p = r.params || {}; const what = p.website_url || p.url || p.user_prompt || "";
      return `<li data-id="${r.request_id}"><span class="badge ${r.status}">${r.status}</span> <b>${esc(r.service)}</b><div class="t">${esc(what).slice(0,70)}</div><div class="t">${new Date(r.created_at*1000).toLocaleString()}${r.duration_s?` · ${r.duration_s}s`:""}</div></li>`;
    }).join("") || `<li class="t">No requests yet.</li>`;
    $("hist").querySelectorAll("li[data-id]").forEach(li => li.onclick = async () => {
      const r = await api(`/v1/requests/${li.dataset.id}`);
      show({...r, ...(r.result && typeof r.result==="object" && !Array.isArray(r.result) ? r.result : {result:r.result}), usage:r.usage, status:r.status, request_id:r.request_id, error:r.error});
    });
  } catch(err){ $("hist").innerHTML = `<li class="err">${esc(err.message)}</li>`; }
}

let authMode = "password";
function setAuthed(on){
  $("app").hidden = !on; $("loginView").hidden = on;
  if (!on) {
    $("keyInput").value = ""; $("passwordInput").value = "";
    $("passwordFields").hidden = authMode !== "password"; $("keyFields").hidden = authMode === "password";
    $("loginHint").textContent = authMode === "password" ? "Self-hosted AI web scraper. Sign in to continue."
      : "No dashboard password is configured (DASHBOARD_EMAIL / DASHBOARD_PASSWORD_HASH), so sign in with the server's API key.";
    (authMode === "password" ? $("emailInput") : $("keyInput")).focus();
  }
}
async function showLogin(){
  try { authMode = (await api("/auth/mode")).mode; } catch {}
  setAuthed(false);
}

async function login(e){
  e.preventDefault(); $("loginErr").textContent = "";
  const btn = $("loginSubmit"); btn.disabled = true;
  try {
    const creds = authMode === "password"
      ? {email:$("emailInput").value.trim(), password:$("passwordInput").value}
      : {key:$("keyInput").value.trim()};
    await api("/auth/login", {method:"POST", body:JSON.stringify(creds)});
    $("keyInput").value = ""; await init();
  } catch(err){ $("loginErr").textContent = err.message; }
  finally { btn.disabled = false; }
}

async function logout(){
  try { await api("/auth/logout", {method:"POST", body:"{}"}); } catch {}
  $("llm").textContent = ""; $("hist").innerHTML = ""; $("out").hidden = true; showLogin();
}

async function init(){
  try { await api("/auth/me"); } catch { showLogin(); return; }
  setAuthed(true);
  try { const s = await api("/v1/status"); const l = s.llm;
    $("llm").textContent = `Claude via ${l.provider==="claude_cli"?"subscription (Claude Code)":l.provider==="anthropic_api"?"API key":l.provider} · ${l.model}`;
  } catch(err){ $("llm").textContent = ""; }
  loadHistory();
}

$("nav").innerHTML = Object.entries(SERVICES).map(([k,s])=>`<button data-s="${k}">${s.name}<small>${s.tag}</small></button>`).join("");
$("nav").addEventListener("click", e => { const b = e.target.closest("button[data-s]"); if (b) select(b.dataset.s); });
$("form").addEventListener("submit", run);
$("loginForm").addEventListener("submit", login);
$("logoutBtn").addEventListener("click", logout);
$("refreshBtn").addEventListener("click", loadHistory);
select(current); init();
