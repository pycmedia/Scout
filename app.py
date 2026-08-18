# ————————————————————————————————————————————————————————————————
# TREND SCOUT — live trend intelligence dashboard for a marketing agency
# Free data sources: Google Trends RSS + Google News RSS (no API keys!)
# Run locally:  pip install -r requirements.txt && python app.py
# ————————————————————————————————————————————————————————————————
import json
import os
import re
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, Response

app = Flask(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefs.json")
STALE_HOURS = 6  # auto-refresh if data older than this when someone opens the page
UA = {"User-Agent": "Mozilla/5.0 (compatible; TrendScout/1.0)"}
HT = "{https://trends.google.com/trending/rss}"
_lock = threading.Lock()

# ——— platform heuristics for hashtag routing ———
LINKEDIN_WORDS = re.compile(
    r"\b(business|market|econom|startup|tech|ai|company|ipo|stock|bank|job|hiring|"
    r"industry|ceo|funding|revenue|brand|linkedin|b2b|finance|invest)\w*", re.I)
ENTERTAIN_WORDS = re.compile(
    r"\b(movie|film|actor|actress|song|music|trailer|cricket|match|ipl|football|"
    r"celebrit|festival|viral|meme|series|show|bollywood|hollywood|award)\w*", re.I)

EVERGREEN = {
    "instagram": ["#Trending", "#Viral", "#Reels", "#InstaDaily", "#ExplorePage"],
    "linkedin": ["#DigitalMarketing", "#MarketingStrategy", "#BrandBuilding",
                 "#SocialMediaMarketing", "#GrowthMarketing"],
    "youtube": ["#Shorts", "#Trending", "#YouTubeIndia"],
}


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def to_hashtag(title):
    words = re.sub(r"[^A-Za-z0-9 ]", " ", title).split()
    tag = "".join(w.capitalize() if not w.isupper() else w for w in words[:4])
    return "#" + tag if tag else ""


def guess_platforms(text):
    if LINKEDIN_WORDS.search(text):
        return ["linkedin", "youtube"]
    if ENTERTAIN_WORDS.search(text):
        return ["instagram", "youtube"]
    return ["instagram"]


def parse_trends(xml_bytes, region):
    out = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        traffic = (item.findtext(HT + "approx_traffic") or "").strip()
        news_el = item.find(HT + "news_item")
        context = ""
        if news_el is not None:
            context = (news_el.findtext(HT + "news_item_title") or "").strip()
        out.append({
            "title": title,
            "traffic": traffic,
            "context": context,
            "region": region,
            "hashtag": to_hashtag(title),
            "platforms": guess_platforms(title + " " + context),
        })
        if len(out) >= 12:
            break
    return out


def parse_news(xml_bytes, category, limit=6):
    out = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        src = item.find("source")
        out.append({
            "headline": title,
            "source": src.text.strip() if src is not None and src.text else "",
            "category": category,
        })
        if len(out) >= limit:
            break
    return out


def try_reddit():
    """Best-effort: Reddit blocks some server IPs; fail silently."""
    try:
        raw = http_get("https://www.reddit.com/r/popular/top.json?t=day&limit=8")
        posts = json.loads(raw)["data"]["children"]
        return [{
            "title": p["data"]["title"][:140],
            "subreddit": "r/" + p["data"]["subreddit"],
            "ups": p["data"].get("ups", 0),
        } for p in posts]
    except Exception:
        return []


def build_brief():
    now = datetime.now(IST)
    trends_in = parse_trends(http_get("https://trends.google.com/trending/rss?geo=IN"), "India")
    trends_global = parse_trends(http_get("https://trends.google.com/trending/rss?geo=US"), "Global")

    news = {
        "marketing": parse_news(http_get(
            "https://news.google.com/rss/search?q=social+media+marketing+OR+advertising+trends&hl=en-IN&gl=IN&ceid=IN:en"
        ), "Marketing"),
        "india": parse_news(http_get(
            "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en"), "India", 5),
        "world": parse_news(http_get(
            "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en"
        ), "World", 5),
    }

    # route hashtags to platforms
    hashtags = {"instagram": [], "linkedin": [], "youtube": []}
    for t in trends_in + trends_global:
        if not t["hashtag"]:
            continue
        entry = {"tag": t["hashtag"], "note": (t["context"] or t["title"])[:70],
                 "traffic": t["traffic"], "region": t["region"]}
        for p in t["platforms"]:
            if len(hashtags[p]) < 8 and entry["tag"] not in [h["tag"] for h in hashtags[p]]:
                hashtags[p].append(entry)
    for p, tags in EVERGREEN.items():
        for tag in tags:
            if len(hashtags[p]) >= 10:
                break
            if tag not in [h["tag"] for h in hashtags[p]]:
                hashtags[p].append({"tag": tag, "note": "evergreen — safe daily use",
                                    "traffic": "", "region": ""})

    return {
        "generated_at": now.isoformat(),
        "date": now.strftime("%A, %d %B %Y"),
        "time": now.strftime("%I:%M %p IST"),
        "trends_in": trends_in,
        "trends_global": trends_global,
        "hashtags": hashtags,
        "news": news,
        "reddit": try_reddit(),
    }


def load_briefs():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_briefs(briefs):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(briefs[:30], f)
    except Exception:
        pass


def latest_brief(force=False):
    with _lock:
        briefs = load_briefs()
        if briefs and not force:
            age = datetime.now(IST) - datetime.fromisoformat(briefs[0]["generated_at"])
            if age < timedelta(hours=STALE_HOURS):
                return briefs[0], briefs
        try:
            brief = build_brief()
            briefs = [brief] + briefs
            save_briefs(briefs)
            return brief, briefs
        except Exception as e:
            if briefs:  # fall back to last good data
                briefs[0]["fetch_error"] = str(e)
                return briefs[0], briefs
            raise


# ————————————————— API —————————————————
@app.route("/api/brief")
def api_brief():
    brief, _ = latest_brief()
    return jsonify(brief)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    brief, _ = latest_brief(force=True)
    return jsonify(brief)


@app.route("/api/history")
def api_history():
    _, briefs = latest_brief()
    return jsonify([{"date": b["date"], "time": b.get("time", ""),
                     "generated_at": b["generated_at"]} for b in briefs])


@app.route("/health")
def health():
    return "ok"


# ————————————————— dashboard —————————————————
@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trend Scout — daily trend intelligence</title>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0C1F22;--panel:#122B2F;--soft:#0F2529;--line:#1E3E43;--text:#E8EDE6;
--muted:#8FA6A3;--amber:#FFB020;--ambersoft:rgba(255,176,32,.12);--coral:#FF6B5E;
--coralsoft:rgba(255,107,94,.12);--mint:#6FD9C3;--mintsoft:rgba(111,217,195,.12)}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 70px}
h1{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:clamp(30px,6vw,44px);line-height:1.02}
h1 span{color:var(--amber)}
.sub{color:var(--muted);font-size:14px;margin-top:6px}
.date{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.topbar{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px}
.btn{font-family:inherit;font-weight:600;font-size:14px;padding:12px 22px;border-radius:10px;
border:1px solid var(--line);background:transparent;color:var(--text);cursor:pointer}
.btn.primary{background:var(--amber);border:none;color:#20180A}
.btn.small{font-size:12px;padding:6px 12px}
.btn:active{transform:scale(.97)}
.btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.ticker{margin-top:20px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
overflow:hidden;white-space:nowrap;padding:10px 0}
.ticker-track{display:inline-flex;gap:28px;animation:tick 32s linear infinite;padding-right:28px;
font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:15px}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media(prefers-reduced-motion:reduce){.ticker-track{animation:none}}
.tabs{display:flex;gap:8px;margin-top:18px;flex-wrap:wrap}
.tab{font-size:13px;font-weight:600;padding:8px 16px;border-radius:999px;border:1px solid var(--line);
background:transparent;color:var(--muted);cursor:pointer;font-family:inherit}
.tab.on{border-color:var(--amber);background:var(--ambersoft);color:var(--amber)}
.label{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--amber);
margin:22px 0 10px;font-weight:600}
.label.coral{color:var(--coral)}.label.mint{color:var(--mint)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}
.grid .panel{margin-bottom:0}
.phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pname{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:15px}
.tagrow{margin-bottom:8px;font-size:13px}
.chip{border-radius:6px;padding:2px 8px;font-weight:600;font-size:13px}
.ig{color:var(--coral);background:var(--coralsoft)}
.li{color:var(--mint);background:var(--mintsoft)}
.yt{color:var(--amber);background:var(--ambersoft)}
.note{color:var(--muted);font-size:12px;margin-left:8px}
.trend{display:flex;gap:14px}
.tnum{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;color:var(--line);min-width:34px}
.ttitle{font-weight:600;font-size:15px}
.tsub{color:var(--muted);font-size:13px;margin-top:3px}
.pill{font-size:11px;font-weight:600;border-radius:999px;padding:2px 10px;margin-right:6px}
.pill.ghost{color:var(--muted);border:1px solid var(--line)}
.match{border-left:3px solid var(--coral)}
.mname{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:15px;color:var(--coral)}
input{font-family:inherit;font-size:14px;background:var(--soft);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;color:var(--text);outline:none;width:100%;margin-bottom:10px}
input::placeholder{color:var(--muted);opacity:.7}
input:focus{border-color:var(--amber)}
.muted{color:var(--muted);font-size:13px}
.small{font-size:12px}
.err{border-color:var(--coral);color:var(--coral)}
.loading{display:flex;align-items:center;gap:12px}
.dot{width:10px;height:10px;border-radius:99px;background:var(--amber);animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
@media(prefers-reduced-motion:reduce){.dot{animation:none}}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
a{color:var(--mint)}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="date" id="dateLine">—</div>
      <h1>Trend Scout<span>.</span></h1>
      <div class="sub">Live daily trends, hashtags &amp; client-ready angles — auto-refreshed.</div>
    </div>
    <button class="btn primary" id="refreshBtn" onclick="refresh()">Refresh now</button>
  </div>

  <div class="ticker"><div id="ticker" class="muted" style="padding-left:4px">Loading live hashtag ticker…</div></div>

  <div class="tabs">
    <button class="tab on" data-tab="today" onclick="show('today')">Today's brief</button>
    <button class="tab" data-tab="clients" onclick="show('clients')">Clients</button>
  </div>

  <div id="status"></div>
  <div id="view-today"></div>

  <div id="view-clients" style="display:none">
    <div class="label">Add a client</div>
    <div class="panel">
      <input id="cName" placeholder="Client name (e.g., FitFuel Gym)">
      <input id="cIndustry" placeholder="Industry (e.g., fitness, real estate, D2C skincare)">
      <input id="cKeywords" placeholder="Keywords, comma separated (e.g., gym, protein, fitness, cricket)">
      <button class="btn primary" onclick="addClient()">Add client</button>
      <div class="muted small" style="margin-top:10px">Clients are saved in this browser. Matching = your keywords found inside today's live trends &amp; news.</div>
    </div>
    <div id="clientList"></div>
  </div>
</div>

<script>
let BRIEF = null;
const PLAT = {instagram:["Instagram / Reels","ig"], linkedin:["LinkedIn","li"], youtube:["YouTube","yt"]};
const esc = s => String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function clients(){ try{return JSON.parse(localStorage.getItem("ts-clients")||"[]")}catch(e){return[]} }
function saveClients(c){ localStorage.setItem("ts-clients", JSON.stringify(c)) }

function show(tab){
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on", t.dataset.tab===tab));
  document.getElementById("view-today").style.display = tab==="today"?"":"none";
  document.getElementById("view-clients").style.display = tab==="clients"?"":"none";
  if(tab==="clients") renderClients();
}

function copyTxt(btn, text){
  navigator.clipboard.writeText(text).then(()=>{ const o=btn.textContent; btn.textContent="Copied!"; setTimeout(()=>btn.textContent=o,1400); });
}

function setStatus(html){ document.getElementById("status").innerHTML = html; }

async function load(force){
  setStatus('<div class="panel loading"><span class="dot"></span><div><b style="font-size:14px">'+
    (force?"Fetching fresh trends…":"Loading today's brief…")+
    '</b><div class="muted small">Live scan of Google Trends + Google News (India + Global)</div></div></div>');
  try{
    const r = await fetch(force?"/api/refresh":"/api/brief", {method: force?"POST":"GET"});
    if(!r.ok) throw new Error("Server said "+r.status);
    BRIEF = await r.json();
    setStatus(BRIEF.fetch_error ? '<div class="panel err">Live fetch hiccup — showing last saved data. Tap Refresh to retry.</div>' : "");
    render();
  }catch(e){
    setStatus('<div class="panel err">Couldn\'t load trends: '+esc(e.message)+'. Tap Refresh to retry.</div>');
  }
}
function refresh(){ load(true) }

function render(){
  const b = BRIEF; if(!b) return;
  document.getElementById("dateLine").textContent = b.date + " · updated " + (b.time||"");
  // ticker
  const tags = Object.values(b.hashtags).flat().map(h=>h.tag);
  const colors = ["var(--amber)","var(--mint)","var(--coral)"];
  document.getElementById("ticker").outerHTML = '<div class="ticker-track" id="ticker">'+
    tags.concat(tags).map((t,i)=>'<span style="color:'+colors[i%3]+'">'+esc(t)+"</span>").join("")+"</div>";

  let h = "";
  // toolbar
  h += '<div class="row" style="margin-top:16px"><button class="btn small" onclick="exportCSV()">⬇ Export CSV (Google Sheets backup)</button>'+
       '<button class="btn small" onclick="copyTxt(this, allTags())">Copy all hashtags</button></div>';

  // hashtags
  h += '<div class="label">Trending hashtags</div><div class="grid">';
  for(const [p, arr] of Object.entries(b.hashtags)){
    const [name, cls] = PLAT[p]||[p,"ig"];
    h += '<div class="panel"><div class="phead"><span class="pname" style="color:var(--'+
      (cls==="ig"?"coral":cls==="li"?"mint":"amber")+')">'+name+'</span>'+
      '<button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(arr.map(x=>x.tag).join(" "))+')\'>Copy set</button></div>';
    for(const t of arr){
      h += '<div class="tagrow"><span class="chip '+cls+'">'+esc(t.tag)+'</span><span class="note">'+
        esc(t.note)+(t.traffic?" · "+esc(t.traffic)+" searches":"")+"</span></div>";
    }
    h += "</div>";
  }
  h += "</div>";

  // trends
  const trendBlock = (title, list) => {
    let s = '<div class="label">'+title+'</div>';
    list.slice(0,8).forEach((t,i)=>{
      s += '<div class="panel trend"><div class="tnum">'+String(i+1).padStart(2,"0")+'</div><div>'+
        '<div class="ttitle">'+esc(t.title)+' <span class="muted small">'+esc(t.traffic)+'</span></div>'+
        (t.context?'<div class="tsub">'+esc(t.context)+"</div>":"")+
        '<div style="margin-top:8px">'+t.platforms.map(p=>{const [n,c]=PLAT[p];return '<span class="pill '+c+'">'+n+"</span>"}).join("")+
        '<span class="pill ghost">'+esc(t.region)+"</span></div></div></div>";
    });
    return s;
  };
  h += trendBlock("Trending in India", b.trends_in);
  h += trendBlock("Trending globally", b.trends_global);

  // client matches
  h += '<div class="label coral">Client matches — from your keywords</div>';
  const matches = computeMatches(b);
  if(clients().length===0){
    h += '<div class="panel muted">Add your clients in the Clients tab — matching runs automatically on every brief.</div>';
  } else if(matches.length===0){
    h += '<div class="panel muted">No keyword hits in today\'s trends. Try broader keywords (e.g., "cricket, food, tech").</div>';
  } else {
    for(const m of matches){
      h += '<div class="panel match"><div class="row"><span class="mname">'+esc(m.client)+'</span>'+
        '<span class="pill '+m.cls+'">'+m.platLabel+'</span></div>'+
        '<div class="tsub">Matched: <b style="color:var(--text)">'+esc(m.hit)+'</b> (keyword: '+esc(m.kw)+')</div>'+
        '<div style="font-size:14px;margin-top:8px">'+esc(m.idea)+'</div>'+
        '<div class="row" style="margin-top:10px"><span class="small" style="color:var(--mint);font-weight:600">'+esc(m.tags.join(" "))+'</span>'+
        '<button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(m.tags.join(" "))+')\'>Copy</button></div></div>';
    }
  }

  // news
  const newsBlock = (title, list, cls) => {
    let s = '<div class="label '+cls+'">'+title+'</div><div class="grid">';
    for(const n of list){ s += '<div class="panel"><div style="font-weight:600;font-size:14px">'+esc(n.headline)+
      '</div><div class="muted small" style="margin-top:6px">'+esc(n.source)+"</div></div>"; }
    return s+"</div>";
  };
  h += newsBlock("Marketing & advertising news", b.news.marketing, "mint");
  h += newsBlock("India headlines", b.news.india, "");
  h += newsBlock("World headlines", b.news.world, "");

  if(b.reddit && b.reddit.length){
    h += '<div class="label">What Reddit is talking about</div>';
    for(const p of b.reddit){ h += '<div class="panel"><span style="font-size:14px">'+esc(p.title)+
      '</span> <span class="muted small">'+esc(p.subreddit)+" · "+p.ups+" upvotes</span></div>"; }
  }
  document.getElementById("view-today").innerHTML = h;
}

function computeMatches(b){
  const out = [];
  const pool = [
    ...b.trends_in.map(t=>({text:t.title+" "+t.context, hit:t.title, tag:t.hashtag, platforms:t.platforms})),
    ...b.trends_global.map(t=>({text:t.title+" "+t.context, hit:t.title, tag:t.hashtag, platforms:t.platforms})),
    ...[].concat(...Object.values(b.news)).map(n=>({text:n.headline, hit:n.headline, tag:"", platforms:["linkedin"]}))
  ];
  for(const c of clients()){
    const kws = (c.keywords||"").split(",").map(k=>k.trim().toLowerCase()).filter(k=>k.length>2);
    for(const kw of kws){
      const found = pool.find(p=>p.text.toLowerCase().includes(kw));
      if(found){
        const p = found.platforms[0]||"instagram";
        const [platLabel, cls] = PLAT[p];
        out.push({client:c.name, kw, hit:found.hit,
          idea:"Idea: create a "+(p==="linkedin"?"post/carousel":"reel/short")+" for "+c.name+
               " riding on \""+found.hit+"\" — tie it to "+(c.industry||"their brand")+" within 24h while it's hot.",
          tags:[found.tag, "#"+(c.name||"").replace(/[^A-Za-z0-9]/g,""), ...(b.hashtags[p]||[]).slice(0,2).map(x=>x.tag)].filter(Boolean),
          platLabel, cls});
        break; // one best match per client
      }
    }
  }
  return out;
}

function allTags(){ return Object.values(BRIEF.hashtags).flat().map(h=>h.tag).join(" ") }

function exportCSV(){
  const b = BRIEF; if(!b) return;
  const esc2 = s => '"'+String(s??"").replace(/"/g,'""')+'"';
  const rows = [["Date","Section","Platform/Client","Item","Detail"]];
  for(const [p,arr] of Object.entries(b.hashtags)) arr.forEach(t=>rows.push([b.date,"Hashtag",p,t.tag,t.note]));
  b.trends_in.concat(b.trends_global).forEach(t=>rows.push([b.date,"Trend",t.region,t.title,t.context]));
  [].concat(...Object.values(b.news)).forEach(n=>rows.push([b.date,"News",n.category,n.headline,n.source]));
  computeMatches(b).forEach(m=>rows.push([b.date,"Client match",m.client,m.hit,m.idea+" | "+m.tags.join(" ")]));
  const blob = new Blob([rows.map(r=>r.map(esc2).join(",")).join("\n")], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "trend-brief-"+new Date().toISOString().slice(0,10)+".csv";
  a.click();
}

function renderClients(){
  const list = clients();
  document.getElementById("clientList").innerHTML = list.length===0 ?
    '<div class="muted" style="text-align:center;padding:12px">No clients yet.</div>' :
    list.map((c,i)=>'<div class="panel row"><div><div class="pname">'+esc(c.name)+'</div>'+
      '<div class="muted small">'+esc([c.industry,c.keywords].filter(Boolean).join(" · "))+'</div></div>'+
      '<button class="btn small" style="color:var(--coral)" onclick="removeClient('+i+')">Remove</button></div>').join("");
}
function addClient(){
  const name = document.getElementById("cName").value.trim(); if(!name) return;
  const list = clients();
  list.push({name, industry:document.getElementById("cIndustry").value.trim(),
             keywords:document.getElementById("cKeywords").value.trim()});
  saveClients(list);
  ["cName","cIndustry","cKeywords"].forEach(id=>document.getElementById(id).value="");
  renderClients(); if(BRIEF) render();
}
function removeClient(i){
  const list = clients(); list.splice(i,1); saveClients(list); renderClients(); if(BRIEF) render();
}

load(false);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
