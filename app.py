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
import urllib.parse
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

    try:
        brand = build_brand(load_clients_srv())
    except Exception:
        brand = []
    return {
        "brand": brand,
        "generated_at": now.isoformat(),
        "date": now.strftime("%A, %d %B %Y"),
        "time": now.strftime("%I:%M %p IST"),
        "trends_in": trends_in,
        "trends_global": trends_global,
        "hashtags": hashtags,
        "news": news,
        "reddit": try_reddit(),
    }



def mark_new(brief, prev):
    """Compare with the previous snapshot and flag anything that wasn't there."""
    if not prev:
        brief["new_count"] = 0
        return
    prev_trends = {t["title"] for t in prev.get("trends_in", []) + prev.get("trends_global", [])}
    prev_tags = {h["tag"] for arr in prev.get("hashtags", {}).values() for h in arr}
    prev_news = {n["headline"] for arr in prev.get("news", {}).values() for n in arr}
    count = 0
    for t in brief.get("trends_in", []) + brief.get("trends_global", []):
        if t["title"] not in prev_trends:
            t["is_new"] = True
            count += 1
    for arr in brief.get("hashtags", {}).values():
        for h in arr:
            if h["tag"] not in prev_tags:
                h["is_new"] = True
                count += 1
    for arr in brief.get("news", {}).values():
        for n in arr:
            if n["headline"] not in prev_news:
                n["is_new"] = True
                count += 1
    prev_brand = {}
    for pb in prev.get("brand", []):
        prev_brand[pb.get("client")] = (
            {k["kw"] for k in pb.get("keywords", [])},
            {t["tag"] for t in pb.get("hashtags", [])},
        )
    for cb in brief.get("brand", []):
        pkw, ptg = prev_brand.get(cb.get("client"), (set(), set()))
        for k in cb.get("keywords", []):
            if k["kw"] not in pkw:
                k["is_new"] = True
                count += 1
        for t in cb.get("hashtags", []):
            if t["tag"] not in ptg:
                t["is_new"] = True
                count += 1
    brief["new_count"] = count


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
            mark_new(brief, briefs[0] if briefs else None)
            briefs = [brief] + briefs
            save_briefs(briefs)
            return brief, briefs
        except Exception as e:
            if briefs:  # fall back to last good data
                briefs[0]["fetch_error"] = str(e)
                return briefs[0], briefs
            raise



# ——— niche engine: per-client-industry live news + proven hashtag banks ———
NICHES = [
    (re.compile(r"school|education|academy|kindergarten|preschool|college|coaching", re.I), {
        "label": "Schools & Education",
        "seeds": ["school admission", "best school"],
        "posts": [["Admissions FAQ carousel", "Answer the 5 questions every parent asks before admission \u2014 one slide each, end with a campus photo."], ["Myth vs Fact", "Bust 4 myths about your board/curriculum. Parents share these \u2014 high save rate."], ["Result & achievement post", "Student wins, toppers, sports medals \u2014 always tag parents' pride. Post within 24h of the event."]],
        "reels": [["POV: first day at school", "Follow one kid from gate to classroom, trending soft audio. Emotional = shares."], ["Teacher takeover reel", "One teacher shows 'a class you wish you had' in 30s \u2014 humanizes the school."], ["Campus tour transition", "Gate \u2192 library \u2192 lab \u2192 playground with beat-drop transitions."]],
        "query": "school education parenting India",
        "tags": ["#School", "#Education", "#Admissions2026", "#ParentingTips", "#StudentLife",
                 "#SchoolLife", "#LearningMadeFun", "#BestSchool", "#KidsEducation", "#FutureReady"],
        "idea": "Reel: 'A day in the life' at {name}, or a carousel answering the #1 question parents ask during admissions season."}),
    (re.compile(r"ice\s?cream|gelato|dessert|kulfi|sundae|frozen", re.I), {
        "label": "Ice Cream & Desserts",
        "seeds": ["ice cream", "ice cream flavours"],
        "query": "ice cream dessert trends India",
        "tags": ["#IceCream", "#IceCreamLover", "#Gelato", "#DessertTime", "#SweetTooth",
                 "#IceCreamShop", "#DessertLovers", "#Kulfi", "#FoodieIndia", "#AhmedabadFoodie"],
        "posts": [["Flavour of the week", "Hero shot of one flavour + its story (ingredients, inspiration). One flavour per post builds craving."],
                  ["This or That poll", "Chocolate vs mango? Cup vs cone? Simple polls get huge comment engagement for dessert brands."],
                  ["Customer moment repost", "Repost customers' stories/photos enjoying Ratel — free content + social proof. Ask permission, tag them."]],
        "reels": [["Slow-mo scoop & drip", "THE ice cream format: slow-mo scoop, sauce drip, or crunch bite with trending audio. 8-second loop."],
                  ["Making-of ASMR", "Behind the scenes: churning, mixing, topping — ASMR sounds on, no music. Dessert ASMR performs 2-3x."],
                  ["Flavour taste-test POV", "Staff or customers blind-taste and rate new flavours on camera — fun, repeatable weekly series."]],
        "idea": "Own the 'treat yourself' moment for {name}: slow-mo scoop reels + weekly flavour spotlights, and push story polls on new flavours."}),
    (re.compile(r"cafe|caf\u00e9|restaurant|food|bakery|coffee|kitchen|eatery|ice\s?cream|dessert|gelato|italian|pizza|pasta", re.I), {
        "label": "Cafes & Food",
        "seeds": ["cafe", "restaurant"],
        "posts": [["Menu spotlight post", "Hero shot of one dish + the story behind it. One dish per post beats collages."], ["This or That", "Cold coffee vs hot cappuccino? Ask followers to vote in comments \u2014 cheap engagement."], ["Customer review screenshot", "Repost a great Google/Zomato review with a thank-you note."]],
        "reels": [["Slow-mo pour / cheese pull", "The #1 food reel format. Trending audio + 8-second loop."], ["Rate my order POV", "Staff or customer rates a full order on screen \u2014 fun, repeatable series."], ["Behind the counter", "Morning prep timelapse: beans, dough, first customer. Authentic > polished."]],
        "query": "cafe food beverage trends India",
        "tags": ["#Cafe", "#CafeVibes", "#FoodieIndia", "#CoffeeLovers", "#CafeHopping",
                 "#InstaFood", "#FoodBlogger", "#CafeAesthetic", "#FoodReels", "#AhmedabadFoodie"],
        "idea": "Reel: slow-mo signature dish + trending audio for {name}; or 'rate my order' POV — food reels with faces get 2-3x reach."}),
    (re.compile(r"interior|decor|furnish|architect|home\s?design|styling|stylist", re.I), {
        "label": "Interior Design & Styling",
        "seeds": ["interior design", "home decor"],
        "posts": [["Before/After carousel", "Slide 1 before, slide 2 after, slides 3-5 details + budget. Highest saves in this niche."], ["3 mistakes post", "'3 mistakes people make with small living rooms' \u2014 position the designer as the fixer."], ["Moodboard Monday", "One palette + 4 product picks. Series format builds a following."]],
        "reels": [["Before/After transition reel", "One-clap or hand-swipe transition from empty to styled room. Must-do format."], ["Client reaction reveal", "Film the client seeing the finished space. Raw emotion outperforms b-roll."], ["60-second design tip", "Talking head: 'Stop buying big sofas for small rooms \u2014 do this instead.'"]],
        "query": "interior design home decor trends India",
        "tags": ["#InteriorDesign", "#HomeDecor", "#InteriorStyling", "#HomeInspo", "#DesignInspiration",
                 "#LuxuryInteriors", "#HomeMakeover", "#InteriorDesignIndia", "#DecorGoals", "#BeforeAndAfter"],
        "idea": "Before/after transformation reel for {name} — the highest-performing format in interiors. Pair with a '3 mistakes people make with small living rooms' carousel."}),
    (re.compile(r"navratri|garba|dandiya|festival|event|wedding|organiser|organizer", re.I), {
        "label": "Navratri & Events",
        "seeds": ["navratri", "garba night"],
        "posts": [["Countdown announcement", "'X days to go' poster series with venue + pass details. Start 3-4 weeks out."], ["Lineup / artist reveal", "Reveal singers/DJs one by one \u2014 each reveal is a separate post."], ["Dress code inspiration", "Chaniya choli color themes per night \u2014 very shareable with friends."]],
        "reels": [["Aftermovie teaser", "Last year's best crowd moments, 15s, big audio. Sells passes instantly."], ["Garba steps tutorial", "Teach one 8-count step per reel \u2014 participants tag friends to practice."], ["Outfit transition", "Day outfit \u2192 garba-night look with a spin transition. Peak Navratri format."]],
        "query": "Navratri garba event Gujarat",
        "tags": ["#Navratri", "#Navratri2026", "#Garba", "#GarbaNight", "#Dandiya",
                 "#NavratriSpecial", "#GarbaLovers", "#FestiveVibes", "#GujaratiCulture", "#EventsAhmedabad"],
        "idea": "Countdown reels + last year's crowd aftermovie for {name}; outfit-transition garba reels trend hard every season — start 3-4 weeks early."}),
    (re.compile(r"real\s?estate|property|realtor|builder|housing|plots?|flats?|consultant", re.I), {
        "label": "Real Estate",
        "seeds": ["real estate", "buy flat"],
        "posts": [["Project highlight carousel", "Slide 1 hook price ('3BHK under \u20b980L?'), then amenities, location map, CTA."], ["Myth-busting post", "'You need 20% down payment' \u2014 false. Trust-building content for consultants."], ["Area guide post", "'Why families are moving to [area]' \u2014 schools, connectivity, appreciation data."]],
        "reels": [["Property walkthrough", "Phone-shot walkthrough with text hooks: 'This costs less than your rent.'"], ["Client handover moment", "Keys-in-hand celebration clip. Social proof that converts."], ["60-second market update", "'What \u20b91 Cr buys in [city] right now' \u2014 weekly talking-head series."]],
        "query": "real estate property market India",
        "tags": ["#RealEstate", "#RealEstateIndia", "#DreamHome", "#PropertyInvestment", "#HomeBuying",
                 "#RealtorLife", "#Property", "#InvestmentTips", "#NewLaunch", "#AhmedabadRealEstate"],
        "idea": "Walkthrough reel with text hooks ('This 3BHK costs less than your rent') for {name}; myth-busting carousels build trust for consultants."}),
    (re.compile(r"personal\s?brand|founder|coach|creator|influencer|linkedin|mentor|speaker", re.I), {
        "label": "Personal Branding",
        "seeds": ["personal branding", "linkedin profile"],
        "posts": [["Contrarian opinion post", "One strong take on your industry. Opinions travel further than tips on LinkedIn."], ["Story post", "A failure \u2192 lesson \u2192 result arc in 150 words. Save-worthy and human."], ["How-to carousel", "'My 5-step process for X' \u2014 repurpose your best advice into slides."]],
        "reels": [["Talking-head hot take", "30s to camera: one opinion, no intro, subtitle everything."], ["Day-in-the-life", "Founder/coach morning-to-meeting montage. Builds parasocial trust."], ["Repurposed post reel", "Turn your top LinkedIn post into a captioned reel \u2014 double the mileage."]],
        "query": "personal branding LinkedIn creator trends",
        "tags": ["#PersonalBranding", "#LinkedInTips", "#BrandYourself", "#ContentCreator", "#ThoughtLeadership",
                 "#FounderLife", "#CareerGrowth", "#LinkedInCreator", "#BuildInPublic", "#Entrepreneurship"],
        "idea": "For {name}: 1 opinion post + 1 story post + 1 how-to carousel per week on LinkedIn; repurpose the best one as a talking-head reel."}),
]

_niche_news_cache = {}

def niche_news(query, limit=4):
    now = datetime.now(IST)
    hit = _niche_news_cache.get(query)
    if hit and now - hit[0] < timedelta(hours=3):
        return hit[1]
    try:
        url = ("https://news.google.com/rss/search?q=" +
               urllib.parse.quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en")
        items = parse_news(http_get(url), "Niche", limit)
    except Exception:
        items = []
    _niche_news_cache[query] = (now, items)
    return items


def detect_niche(text):
    for pattern, cfg in NICHES:
        if pattern.search(text):
            return cfg
    return None


@app.route("/api/niche", methods=["POST"])
def api_niche():
    from flask import request as _rq
    data = _rq.get_json(force=True, silent=True) or {}
    out = []
    for c in (data.get("clients") or [])[:15]:
        name = (c.get("name") or "").strip()
        blob = " ".join([name, c.get("industry") or "", c.get("keywords") or ""])
        cfg = detect_niche(blob)
        if cfg:
            out.append({
                "client": name, "niche": cfg["label"],
                "hashtags": cfg["tags"],
                "news": niche_news(cfg["query"]),
                "idea": cfg["idea"].format(name=name or "this client"),
                "posts": cfg.get("posts", []),
                "reels": cfg.get("reels", []),
            })
        else:
            q = (c.get("industry") or c.get("keywords") or name).strip()
            out.append({
                "client": name, "niche": (c.get("industry") or "Custom").title(),
                "hashtags": [to_hashtag(w) for w in q.split(",")[:4] if w.strip()] +
                            ["#Trending", "#Reels", "#DigitalMarketing"],
                "news": niche_news(q + " India trends") if q else [],
                "idea": f"Ride this week's biggest headline in the {q or 'client'} space with a quick opinion reel for {name or 'this client'}.",
                "posts": [["Trend reaction post", "Take this week's biggest headline in this niche and share your client's take on it."],
                          ["FAQ carousel", "Answer the 5 most-asked customer questions, one slide each."],
                          ["Social proof post", "Screenshot a great review or client message with a thank-you note."]],
                "reels": [["Behind the scenes", "30s of the real daily work — authenticity outperforms polish."],
                          ["3 quick tips", "Talking head: three tips in this niche, subtitled, under 40s."],
                          ["Before/After or reveal", "Show a transformation or final result with trending audio."]],
            })
    return jsonify(out)




# ——— brand tags engine: live search keywords + hashtags per client brand ———
_suggest_cache = {}
STOPWORDS = set("""the a an and or of for to in on at with from by is are was were be been this that
these those it its as but not no new news india indian says say said will can could would should
your you our we they he she his her their them what when where which who why how all more most
over under after before during between amid vs versus into out up down off than then now today
year years 2024 2025 2026 2027 day week month top best latest breaking update updates live
""".split())


def google_suggest(seed):
    """Live 'what people are searching right now' — cached 3h."""
    now = datetime.now(IST)
    hit = _suggest_cache.get(seed)
    if hit and now - hit[0] < timedelta(hours=3):
        return hit[1]
    out = []
    try:
        url = ("https://suggestqueries.google.com/complete/search?client=firefox&hl=en&gl=in&q="
               + urllib.parse.quote(seed))
        data = json.loads(http_get(url).decode("utf-8", "ignore"))
        pat = re.compile(r"\b" + re.escape(seed.split()[0]) + r"\b", re.I)
        for s in data[1][:8]:
            s = s.strip()
            if s and s.lower() != seed.lower() and pat.search(s):
                out.append(s)
    except Exception:
        pass
    _suggest_cache[seed] = (now, out[:5])
    return out[:5]


def news_keywords(headlines, limit=5):
    """Pull the hottest phrases out of fresh niche headlines."""
    from collections import Counter
    uni, bi = Counter(), Counter()
    for hl in headlines:
        words = [w for w in re.findall(r"[a-zA-Z]{3,}", hl.lower()) if w not in STOPWORDS]
        uni.update(words)
        bi.update(zip(words, words[1:]))
    out = [" ".join(p) for p, c in bi.most_common(limit) if c >= 2]
    for w, c in uni.most_common(limit * 2):
        if len(out) >= limit:
            break
        if c >= 2 and all(w not in o for o in out):
            out.append(w)
    return out[:limit]


def build_brand(clients_list):
    out = []
    for c in clients_list[:15]:
        name = (c.get("name") or "").strip()
        blob = " ".join([name, c.get("industry") or "", c.get("keywords") or ""])
        cfg = detect_niche(blob)
        seeds = list(cfg.get("seeds", [])) if cfg else []
        first_kw = ((c.get("keywords") or "").split(",")[0] or "").strip()
        if first_kw and first_kw.lower() not in [s.lower() for s in seeds]:
            seeds.append(first_kw)
        keywords, seen = [], set()
        for seed in seeds[:3]:
            for s in google_suggest(seed):
                if s.lower() not in seen:
                    seen.add(s.lower())
                    keywords.append({"kw": s, "src": "search"})
        if cfg:
            heads = [n["headline"] for n in niche_news(cfg["query"], 6)]
            for k in news_keywords(heads, 4):
                if k.lower() not in seen:
                    seen.add(k.lower())
                    keywords.append({"kw": k, "src": "news"})
        keywords = keywords[:10]
        tags, tseen = [], set()
        for k in keywords:
            t = to_hashtag(k["kw"])
            if t and len(t) > 3 and t.lower() not in tseen:
                tseen.add(t.lower())
                tags.append({"tag": t})
        for t in (cfg["tags"][:4] if cfg else []):
            if t.lower() not in tseen and len(tags) < 12:
                tseen.add(t.lower())
                tags.append({"tag": t})
        out.append({"client": name, "niche": cfg["label"] if cfg else (c.get("industry") or "Custom").title(),
                    "keywords": keywords, "hashtags": tags})
    return out


# ——— shared clients (same list for every device that opens the site) ———
CLIENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients.json")

# Pre-loaded so every device starts ready — edit names to your real clients in the dashboard
DEFAULT_CLIENTS = [
    {"name": "Divine Bliss International School", "industry": "school, education",
     "keywords": "school, admissions, parents, students, education, kids"},
    {"name": "Divine Values International School", "industry": "school, education",
     "keywords": "school, admissions, parents, students, education, values"},
    {"name": "Cicahda — Italian Cafe", "industry": "italian cafe, restaurant",
     "keywords": "italian food, pasta, pizza, coffee, cafe, dining"},
    {"name": "Evara — Interior Stylist", "industry": "interior styling",
     "keywords": "interior styling, home decor, styling, makeover"},
    {"name": "CP Design Studio — Interior Designer", "industry": "interior design",
     "keywords": "interior design, home interiors, design, renovation"},
    {"name": "C Cure Consultants", "industry": "real estate consultant",
     "keywords": "real estate, property, investment, consultant"},
    {"name": "Ratel Ice Cream", "industry": "ice cream, desserts",
     "keywords": "ice cream, dessert, gelato, sweet, flavours"},
]


def load_clients_srv():
    try:
        with open(CLIENTS_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    seed = os.environ.get("CLIENTS_SEED", "").strip()
    if seed:  # optional: "Name|industry|keywords;Name2|..."
        out = []
        for part in seed.split(";"):
            bits = (part.split("|") + ["", ""])[:3]
            if bits[0].strip():
                out.append({"name": bits[0].strip(), "industry": bits[1].strip(),
                            "keywords": bits[2].strip()})
        if out:
            return out
    return [dict(c) for c in DEFAULT_CLIENTS]


def save_clients_srv(cl):
    try:
        with open(CLIENTS_FILE, "w") as f:
            json.dump(cl[:30], f)
    except Exception:
        pass


@app.route("/api/clients", methods=["GET"])
def api_clients():
    return jsonify(load_clients_srv())


@app.route("/api/clients/add", methods=["POST"])
def api_clients_add():
    from flask import request as _rq
    data = _rq.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(load_clients_srv())
    cl = load_clients_srv()
    cl.append({"name": name[:60], "industry": (data.get("industry") or "").strip()[:80],
               "keywords": (data.get("keywords") or "").strip()[:120]})
    save_clients_srv(cl)
    return jsonify(cl)


@app.route("/api/clients/remove", methods=["POST"])
def api_clients_remove():
    from flask import request as _rq
    data = _rq.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    cl = load_clients_srv()
    if isinstance(idx, int) and 0 <= idx < len(cl):
        cl.pop(idx)
        save_clients_srv(cl)
    return jsonify(cl)


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
    return jsonify([{
        "i": i, "date": b["date"], "time": b.get("time", ""),
        "new_count": b.get("new_count", 0),
        "trends": len(b.get("trends_in", [])) + len(b.get("trends_global", [])),
        "hashtags": sum(len(a) for a in b.get("hashtags", {}).values()),
        "news": sum(len(a) for a in b.get("news", {}).values()),
    } for i, b in enumerate(briefs)])


@app.route("/api/history/item")
def api_history_item():
    from flask import request as _rq
    _, briefs = latest_brief()
    try:
        i = int(_rq.args.get("i", 0))
    except Exception:
        i = 0
    i = max(0, min(i, len(briefs) - 1))
    return jsonify(dict(briefs[i], history_index=i))


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
:root{--bg:#0A1B1E;--panel:rgba(23,48,53,.55);--soft:#0F2529;--line:#20444A;
--text:#EAF0EA;--muted:#8FA6A3;--amber:#FFB020;--ambersoft:rgba(255,176,32,.13);
--coral:#FF6B5E;--coralsoft:rgba(255,107,94,.13);--mint:#6FD9C3;--mintsoft:rgba(111,217,195,.13)}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif;min-height:100vh;
background-image:radial-gradient(600px 400px at 85% -50px,rgba(255,176,32,.14),transparent 70%),
radial-gradient(700px 500px at -10% 20%,rgba(111,217,195,.10),transparent 70%),
radial-gradient(600px 600px at 100% 100%,rgba(255,107,94,.08),transparent 70%);
background-attachment:fixed}
.wrap{max-width:1000px;margin:0 auto;padding:26px 20px 80px}
h1{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:clamp(30px,6vw,46px);line-height:1.02;
background:linear-gradient(92deg,#EAF0EA 60%,#FFB020);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:14px;margin-top:6px}
.date{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);margin-bottom:6px;display:flex;align-items:center;gap:8px}
.live{display:inline-flex;align-items:center;gap:5px;color:var(--mint);font-weight:600}
.live .dot{width:7px;height:7px}
.topbar{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px}
.btn{font-family:inherit;font-weight:600;font-size:14px;padding:12px 22px;border-radius:12px;
border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--text);cursor:pointer;transition:all .15s}
.btn:hover{border-color:var(--amber);transform:translateY(-1px)}
.btn.primary{background:linear-gradient(135deg,#FFB020,#FF8A3C);border:none;color:#20180A;box-shadow:0 4px 20px rgba(255,176,32,.25)}
.btn.small{font-size:12px;padding:6px 12px;border-radius:9px}
.btn:active{transform:scale(.97)}
.btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.ticker{margin-top:20px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
overflow:hidden;white-space:nowrap;padding:10px 0}
.ticker-track{display:inline-flex;gap:28px;animation:tick 32s linear infinite;padding-right:28px;
font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:15px}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media(prefers-reduced-motion:reduce){.ticker-track{animation:none}}
.tabbar{position:sticky;top:0;z-index:50;background:rgba(10,27,30,.85);backdrop-filter:blur(12px);
margin:0 -20px;padding:12px 20px;display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--line)}
.tabbar::-webkit-scrollbar{display:none}
.tab{font-size:13px;font-weight:600;padding:9px 16px;border-radius:999px;border:1px solid var(--line);
background:transparent;color:var(--muted);cursor:pointer;font-family:inherit;white-space:nowrap;transition:all .15s}
.tab.on{border-color:var(--amber);background:var(--ambersoft);color:var(--amber);box-shadow:0 0 16px rgba(255,176,32,.15)}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 16px;backdrop-filter:blur(8px)}
.stat b{font-family:'Bricolage Grotesque',sans-serif;font-size:20px;font-weight:800;color:var(--amber)}
.stat span{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-top:2px}
.label{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--amber);
margin:26px 0 12px;font-weight:600;display:flex;align-items:center;gap:8px}
.label:before{content:"";width:18px;height:2px;background:currentColor;border-radius:2px}
.label.coral{color:var(--coral)}.label.mint{color:var(--mint)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:10px;
backdrop-filter:blur(8px);transition:transform .15s,border-color .15s}
.panel.hov:hover{transform:translateY(-2px);border-color:rgba(255,176,32,.4)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.grid .panel{margin-bottom:0}
.phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pname{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:15px}
.tagrow{margin-bottom:8px;font-size:13px}
.chip{border-radius:7px;padding:2px 9px;font-weight:600;font-size:13px}
.ig{color:var(--coral);background:var(--coralsoft)}
.li{color:var(--mint);background:var(--mintsoft)}
.yt{color:var(--amber);background:var(--ambersoft)}
.note{color:var(--muted);font-size:12px;margin-left:8px}
.trend{display:flex;gap:14px}
.tnum{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:22px;
background:linear-gradient(180deg,#3A6A72,#1E3E43);-webkit-background-clip:text;background-clip:text;color:transparent;min-width:36px}
.ttitle{font-weight:600;font-size:15px}
.tsub{color:var(--muted);font-size:13px;margin-top:3px}
.pill{font-size:11px;font-weight:600;border-radius:999px;padding:2px 10px;margin-right:6px}
.pill.ghost{color:var(--muted);border:1px solid var(--line)}
.newpill{font-size:10px;font-weight:800;letter-spacing:.08em;color:#20180A;background:linear-gradient(135deg,#FFB020,#FF6B5E);
border-radius:6px;padding:2px 7px;margin-left:8px;vertical-align:2px;animation:glowNew 1.6s ease-in-out infinite}
@keyframes glowNew{0%,100%{box-shadow:0 0 4px rgba(255,176,32,.4)}50%{box-shadow:0 0 12px rgba(255,107,94,.7)}}
@media(prefers-reduced-motion:reduce){.newpill{animation:none}}
.pastbanner{background:var(--ambersoft);border:1px solid var(--amber);border-radius:12px;padding:12px 16px;
margin-top:16px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.match{border-left:3px solid var(--coral)}
.mname{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:16px;color:var(--coral)}
.idea{display:flex;gap:14px;align-items:flex-start}
.ibadge{min-width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;
font-size:16px;background:var(--ambersoft);flex-shrink:0}
.idea.reelcard .ibadge{background:var(--coralsoft)}
.ititle{font-weight:600;font-size:15px}
.idetail{color:var(--muted);font-size:13px;margin-top:3px;line-height:1.5}
input{font-family:inherit;font-size:14px;background:var(--soft);border:1px solid var(--line);
border-radius:11px;padding:11px 13px;color:var(--text);outline:none;width:100%;margin-bottom:10px}
input::placeholder{color:var(--muted);opacity:.7}
input:focus{border-color:var(--amber);box-shadow:0 0 0 3px rgba(255,176,32,.12)}
.muted{color:var(--muted);font-size:13px}
.small{font-size:12px}
.err{border-color:var(--coral);color:var(--coral)}
.loading{display:flex;align-items:center;gap:12px}
.dot{width:10px;height:10px;border-radius:99px;background:var(--amber);animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
@media(prefers-reduced-motion:reduce){.dot{animation:none}}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.clienthead{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:17px;margin:20px 0 10px;
display:flex;align-items:center;gap:10px}
.clienthead .pill{font-family:'Instrument Sans'}
a{color:var(--mint)}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="date"><span id="dateLine">—</span><span class="live"><span class="dot"></span>LIVE</span></div>
      <h1>Trend Scout.</h1>
      <div class="sub">Live trends → hashtags → post &amp; reel ideas, matched to your clients.</div>
    </div>
    <button class="btn primary" id="refreshBtn" onclick="refresh()">↻ Refresh now</button>
  </div>

  <div class="ticker"><div id="ticker" class="muted" style="padding-left:4px">Loading live hashtag ticker…</div></div>

  <div class="stats" id="stats"></div>

  <div class="tabbar" id="tabbar">
    <button class="tab on" data-tab="today" onclick="show('today')">📊 Today</button>
    <button class="tab" data-tab="posts" onclick="show('posts')">📝 Post ideas</button>
    <button class="tab" data-tab="reels" onclick="show('reels')">🎬 Reel ideas</button>
    <button class="tab" data-tab="brand" onclick="show('brand')">🏷️ Brand tags</button>
    <button class="tab" data-tab="playbook" onclick="show('playbook')">🎯 Client playbook</button>
    <button class="tab" data-tab="history" onclick="show('history')">🕘 History</button>
    <button class="tab" data-tab="clients" onclick="show('clients')">👥 Clients</button>
  </div>

  <div id="status"></div>
  <div id="view-today"></div>
  <div id="view-posts" style="display:none"></div>
  <div id="view-reels" style="display:none"></div>
  <div id="view-brand" style="display:none"></div>
  <div id="view-playbook" style="display:none"></div>
  <div id="view-history" style="display:none"></div>

  <div id="view-clients" style="display:none">
    <div class="label">Add a client</div>
    <div class="panel">
      <div class="muted small" style="margin-bottom:8px">Quick add — tap to prefill:</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
        <button class="tab" onclick="prefill('School','school, education','admissions, parents, students, school')">School</button>
        <button class="tab" onclick="prefill('Cafe','cafe, food','cafe, coffee, food, restaurant')">Cafe</button>
        <button class="tab" onclick="prefill('Interior Designer','interior design','interior, home decor, design')">Interior designer</button>
        <button class="tab" onclick="prefill('Interior Stylist','interior styling','interior, styling, decor')">Interior stylist</button>
        <button class="tab" onclick="prefill('Navratri Organizer','navratri events','navratri, garba, dandiya, event')">Navratri organizer</button>
        <button class="tab" onclick="prefill('Real Estate Consultant','real estate consultant','real estate, property, investment')">Real estate</button>
        <button class="tab" onclick="prefill('Personal Brand Client','personal branding','personal brand, linkedin, founder')">Personal branding</button>
      </div>
      <input id="cName" placeholder="Client name (e.g., Sunrise School)">
      <input id="cIndustry" placeholder="Industry (e.g., interior design, real estate)">
      <input id="cKeywords" placeholder="Keywords, comma separated (e.g., garba, navratri, event)">
      <button class="btn primary" onclick="addClient()">Add client</button>
      <div class="muted small" style="margin-top:10px">Clients are shared — everyone who opens this link sees the same list on every device. They power the Post ideas, Reel ideas &amp; Playbook tabs.</div>
    </div>
    <div id="clientList"></div>
  </div>
</div>

<script>
let BRIEF = null, NICHE = null, CLIENTS = [], LIVE_BRIEF = null, PAST_INDEX = -1;
const PLAT = {instagram:["Instagram / Reels","ig"], linkedin:["LinkedIn","li"], youtube:["YouTube","yt"]};
const esc = s => String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function clients(){ return CLIENTS }
async function syncClients(){
  try{ const r = await fetch("/api/clients"); CLIENTS = await r.json(); }catch(e){ CLIENTS = CLIENTS||[]; }
}

function show(tab){
  document.querySelectorAll(".tabbar .tab").forEach(t=>t.classList.toggle("on", t.dataset.tab===tab));
  ["today","posts","reels","brand","playbook","history","clients"].forEach(v=>{
    document.getElementById("view-"+v).style.display = v===tab?"":"none";
  });
  if(tab==="clients") renderClients();
  if(tab==="brand") renderBrand();
  if(tab==="history") renderHistory();
  if(tab==="posts") renderPosts();
  if(tab==="reels") renderReels();
  if(tab==="playbook") renderPlaybook();
}

function copyTxt(btn, text){
  navigator.clipboard.writeText(text).then(()=>{ const o=btn.textContent; btn.textContent="Copied!"; setTimeout(()=>btn.textContent=o,1400); });
}
function setStatus(html){ document.getElementById("status").innerHTML = html; }

async function load(force){
  await syncClients();
  setStatus('<div class="panel loading"><span class="dot"></span><div><b style="font-size:14px">'+
    (force?"Fetching fresh trends…":"Loading today's brief…")+
    '</b><div class="muted small">Live scan of Google Trends + Google News (India + Global)</div></div></div>');
  try{
    const r = await fetch(force?"/api/refresh":"/api/brief", {method: force?"POST":"GET"});
    if(!r.ok) throw new Error("Server said "+r.status);
    BRIEF = await r.json(); LIVE_BRIEF = BRIEF; PAST_INDEX = -1;
    setStatus(BRIEF.fetch_error ? '<div class="panel err">Live fetch hiccup — showing last saved data. Tap Refresh to retry.</div>' : "");
    renderToday();
    loadNiche(true);
  }catch(e){
    setStatus('<div class="panel err">Couldn\'t load trends: '+esc(e.message)+'. Tap Refresh to retry.</div>');
  }
}
function refresh(){ NICHE=null; load(true) }

async function loadNiche(rerenderCurrent){
  if(clients().length===0){ NICHE=[]; return; }
  try{
    const r = await fetch("/api/niche", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({clients: clients()})});
    NICHE = await r.json();
  }catch(e){ NICHE = []; }
  if(rerenderCurrent){
    const on = document.querySelector(".tabbar .tab.on");
    if(on && on.dataset.tab!=="today" && on.dataset.tab!=="clients") show(on.dataset.tab);
  }
}

function topTrends(n){
  if(!BRIEF) return [];
  return BRIEF.trends_in.slice(0, Math.ceil(n/2)).concat(BRIEF.trends_global.slice(0, Math.floor(n/2)));
}
function allTags(){ return Object.values(BRIEF.hashtags).flat().map(h=>h.tag).join(" ") }

// ————— TODAY —————
function renderToday(){
  const b = BRIEF; if(!b) return;
  document.getElementById("dateLine").textContent = b.date + " · updated " + (b.time||"");
  const tags = Object.values(b.hashtags).flat().map(h=>h.tag);
  const colors = ["var(--amber)","var(--mint)","var(--coral)"];
  document.getElementById("ticker").outerHTML = '<div class="ticker-track" id="ticker">'+
    tags.concat(tags).map((t,i)=>'<span style="color:'+colors[i%3]+'">'+esc(t)+"</span>").join("")+"</div>";

  const nTrends = b.trends_in.length + b.trends_global.length;
  const nNews = Object.values(b.news).reduce((a,l)=>a+l.length,0);
  document.getElementById("stats").innerHTML =
    '<div class="stat"><b>'+nTrends+'</b><span>live trends</span></div>'+
    '<div class="stat"><b>'+tags.length+'</b><span>hashtags</span></div>'+
    '<div class="stat"><b>'+nNews+'</b><span>news angles</span></div>'+
    '<div class="stat"><b>'+clients().length+'</b><span>clients tracked</span></div>';

  let h = "";
  if(PAST_INDEX > -1){
    h += '<div class="pastbanner"><span>🕘 Viewing past snapshot — <b>'+esc(b.date)+' · '+esc(b.time||"")+'</b></span>'+
         '<button class="btn small primary" onclick="backToLive()">⬅ Back to live</button></div>';
  } else if(b.new_count > 0){
    h += '<div class="pastbanner"><span>✨ <b>'+b.new_count+' new items</b> since your last refresh — look for the NEW tags below.</span></div>';
  }
  h += '<div class="row" style="margin-top:16px"><button class="btn small" onclick="exportCSV()">⬇ Export CSV (Google Sheets)</button>'+
       '<button class="btn small" onclick="copyTxt(this, allTags())">Copy all hashtags</button></div>';

  h += '<div class="label"># Trending hashtags</div><div class="grid">';
  for(const [p, arr] of Object.entries(b.hashtags)){
    const [name, cls] = PLAT[p]||[p,"ig"];
    h += '<div class="panel hov"><div class="phead"><span class="pname" style="color:var(--'+
      (cls==="ig"?"coral":cls==="li"?"mint":"amber")+')">'+name+'</span>'+
      '<button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(arr.map(x=>x.tag).join(" "))+')\'>Copy set</button></div>';
    for(const t of arr){
      h += '<div class="tagrow"><span class="chip '+cls+'">'+esc(t.tag)+'</span>'+
        (t.is_new?'<span class="newpill">NEW</span>':'')+'<span class="note">'+
        esc(t.note)+(t.traffic?" · "+esc(t.traffic)+" searches":"")+"</span></div>";
    }
    h += "</div>";
  }
  h += "</div>";

  const trendBlock = (title, list) => {
    let s = '<div class="label">'+title+'</div>';
    list.slice(0,8).forEach((t,i)=>{
      s += '<div class="panel hov trend"><div class="tnum">'+String(i+1).padStart(2,"0")+'</div><div>'+
        '<div class="ttitle">'+esc(t.title)+(t.is_new?'<span class="newpill">NEW</span>':'')+' <span class="muted small">'+esc(t.traffic)+'</span></div>'+
        (t.context?'<div class="tsub">'+esc(t.context)+"</div>":"")+
        '<div style="margin-top:8px">'+t.platforms.map(p=>{const [n,c]=PLAT[p];return '<span class="pill '+c+'">'+n+"</span>"}).join("")+
        '<span class="pill ghost">'+esc(t.region)+"</span></div></div></div>";
    });
    return s;
  };
  h += trendBlock("🇮🇳 Trending in India", b.trends_in);
  h += trendBlock("🌍 Trending globally", b.trends_global);

  const newsBlock = (title, list, cls) => {
    let s = '<div class="label '+cls+'">'+title+'</div><div class="grid">';
    for(const n of list){ s += '<div class="panel hov"><div style="font-weight:600;font-size:14px">'+esc(n.headline)+
      (n.is_new?'<span class="newpill">NEW</span>':'')+
      '</div><div class="muted small" style="margin-top:6px">'+esc(n.source)+"</div></div>"; }
    return s+"</div>";
  };
  h += newsBlock("📣 Marketing & advertising news", b.news.marketing, "mint");
  h += newsBlock("🗞️ India headlines", b.news.india, "");
  h += newsBlock("🌐 World headlines", b.news.world, "");

  if(b.reddit && b.reddit.length){
    h += '<div class="label">👽 What Reddit is talking about</div>';
    for(const p of b.reddit){ h += '<div class="panel"><span style="font-size:14px">'+esc(p.title)+
      '</span> <span class="muted small">'+esc(p.subreddit)+" · "+p.ups+" upvotes</span></div>"; }
  }
  document.getElementById("view-today").innerHTML = h;
}

// ————— IDEA TABS (posts / reels) —————
function ideaCard(icon, title, detail, cls){
  return '<div class="panel hov idea '+(cls||"")+'"><div class="ibadge">'+icon+'</div><div style="flex:1">'+
    '<div class="ititle">'+esc(title)+'</div><div class="idetail">'+esc(detail)+'</div></div>'+
    '<button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(title+" — "+detail)+')\'>Copy</button></div>';
}

function generalIdeas(kind){
  const t = topTrends(4);
  if(kind==="posts") return t.map(x=>["Trend take: "+x.title,
    "Opinion or reaction post connecting \""+x.title+"\" to your client's world. Post within 24h while it's hot. ("+x.region+" trend"+(x.traffic?", "+x.traffic+" searches":"")+")"]);
  return t.map(x=>["React to: "+x.title,
    "Green-screen or talking-head reel reacting to \""+x.title+"\" — hook in the first 2 seconds, subtitles on. ("+x.region+" trend)"]);
}

function renderIdeaTab(kind){
  const icon = kind==="posts" ? "📝" : "🎬";
  const cls = kind==="posts" ? "" : "reelcard";
  let h = '<div class="label">'+icon+' '+(kind==="posts"?"Post ideas from today's trends":"Reel ideas from today's trends")+'</div>';
  if(!BRIEF){ h += '<div class="panel muted">Loading today\'s trends…</div>'; }
  else generalIdeas(kind).forEach(([t,d])=>{ h += ideaCard(icon,t,d,cls); });

  h += '<div class="label coral">'+icon+' '+(kind==="posts"?"Post ideas for your clients":"Reel ideas for your clients")+'</div>';
  if(clients().length===0){
    h += '<div class="panel muted">Add clients in the 👥 Clients tab to unlock niche-specific '+(kind==="posts"?"post":"reel")+' ideas.</div>';
  } else if(!NICHE){
    h += '<div class="panel loading"><span class="dot"></span><span class="muted">Building ideas for each client…</span></div>';
  } else {
    for(const m of NICHE){
      h += '<div class="clienthead">'+esc(m.client)+' <span class="pill ghost">'+esc(m.niche)+'</span></div>';
      (m[kind]||[]).forEach(([t,d])=>{ h += ideaCard(icon,t,d,cls); });
    }
  }
  return h;
}
function renderPosts(){ document.getElementById("view-posts").innerHTML = renderIdeaTab("posts"); }
function renderReels(){ document.getElementById("view-reels").innerHTML = renderIdeaTab("reels"); }

// ————— PLAYBOOK —————
function renderPlaybook(){
  const el = document.getElementById("view-playbook");
  if(clients().length===0){
    el.innerHTML = '<div class="label coral">🎯 Client playbook</div><div class="panel muted">Add your clients in the 👥 Clients tab — each gets live niche news + a proven hashtag bank here.</div>';
    return;
  }
  if(!NICHE){
    el.innerHTML = '<div class="label coral">🎯 Client playbook</div><div class="panel loading"><span class="dot"></span><span class="muted">Fetching live news for each client\'s niche…</span></div>';
    return;
  }
  let s = '<div class="label coral">🎯 Client playbook — niche trends & hashtags</div>';
  for(const m of NICHE){
    s += '<div class="panel match"><div class="row"><span class="mname">'+esc(m.client)+'</span><span class="pill ghost">'+esc(m.niche)+'</span></div>';
    s += '<div style="font-size:14px;margin-top:8px">💡 '+esc(m.idea)+'</div>';
    if(m.news && m.news.length){
      s += '<div class="tsub" style="margin-top:10px;font-weight:600;color:var(--mint)">Fresh angles in this niche:</div>';
      for(const n of m.news){ s += '<div class="tsub">• '+esc(n.headline)+' <span class="small">('+esc(n.source)+')</span></div>'; }
    }
    s += '<div class="row" style="margin-top:10px"><span class="small" style="color:var(--mint);font-weight:600">'+esc(m.hashtags.join(" "))+'</span>'+
         '<button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(m.hashtags.join(" "))+')\'>Copy hashtags</button></div></div>';
  }
  el.innerHTML = s;
}

// ————— BRAND TAGS —————
function renderBrand(){
  const el = document.getElementById("view-brand");
  if(!BRIEF){ el.innerHTML = '<div class="panel loading"><span class="dot"></span><span class="muted">Loading…</span></div>'; return; }
  const data = BRIEF.brand || [];
  let s = "";
  if(PAST_INDEX > -1){
    s += '<div class="pastbanner"><span>🕘 Brand tags from <b>'+esc(BRIEF.date)+' · '+esc(BRIEF.time||"")+'</b></span>'+
         '<button class="btn small primary" onclick="backToLive()">⬅ Back to live</button></div>';
  }
  s += '<div class="label">🏷️ Brand tags — live keywords & hashtags per client</div>'+
       '<div class="muted small" style="margin-bottom:14px">🔍 = what people are searching right now (Google) · 📰 = hot phrases from this niche\'s news. Refresh to catch new ones — fresh finds get a NEW tag, old sets stay in 🕘 History.</div>';
  if(data.length===0){
    s += '<div class="panel muted">Brand tags appear after the next refresh — tap ↻ Refresh now.</div>';
  }
  for(const b of data){
    s += '<div class="panel match"><div class="row"><span class="mname">'+esc(b.client)+'</span><span class="pill ghost">'+esc(b.niche)+'</span></div>';
    if(b.keywords && b.keywords.length){
      s += '<div class="tsub" style="margin-top:10px;font-weight:600;color:var(--mint)">Trending keywords</div><div style="margin-top:6px">';
      for(const k of b.keywords){
        s += '<span class="chip li" style="display:inline-block;margin:0 6px 6px 0">'+(k.src==="search"?"🔍 ":"📰 ")+esc(k.kw)+'</span>'+(k.is_new?'<span class="newpill">NEW</span> ':' ');
      }
      s += '</div><button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(b.keywords.map(k=>k.kw).join(", "))+')\'>Copy keywords</button>';
    }
    if(b.hashtags && b.hashtags.length){
      s += '<div class="tsub" style="margin-top:14px;font-weight:600;color:var(--amber)">Hashtags</div><div style="margin-top:6px">';
      for(const t of b.hashtags){
        s += '<span class="chip yt" style="display:inline-block;margin:0 6px 6px 0">'+esc(t.tag)+'</span>'+(t.is_new?'<span class="newpill">NEW</span> ':' ');
      }
      s += '</div><button class="btn small" onclick=\'copyTxt(this,'+JSON.stringify(b.hashtags.map(t=>t.tag).join(" "))+')\'>Copy hashtags</button>';
    }
    s += '</div>';
  }
  el.innerHTML = s;
}

// ————— HISTORY —————
async function renderHistory(){
  const el = document.getElementById("view-history");
  el.innerHTML = '<div class="label">🕘 History</div><div class="panel loading"><span class="dot"></span><span class="muted">Loading snapshots…</span></div>';
  try{
    const r = await fetch("/api/history");
    const items = await r.json();
    let s = '<div class="label">🕘 History — every refresh is saved (last 30)</div>';
    if(items.length<=1){ s += '<div class="panel muted">Past snapshots will appear here after your next refresh.</div>'; }
    items.forEach((b)=>{
      s += '<div class="panel hov row"><div><div class="pname">'+esc(b.date)+' <span class="muted small">· '+esc(b.time)+'</span>'+
        (b.i===0?'<span class="newpill">LATEST</span>':'')+'</div>'+
        '<div class="muted small" style="margin-top:2px">'+b.trends+' trends · '+b.hashtags+' hashtags · '+b.news+' news'+
        (b.new_count?' · <b style="color:var(--amber)">'+b.new_count+' new</b>':'')+'</div></div>'+
        '<button class="btn small" onclick="viewPast('+b.i+')">View</button></div>';
    });
    el.innerHTML = s;
  }catch(e){
    el.innerHTML = '<div class="panel err">Couldn\'t load history — try again.</div>';
  }
}
async function viewPast(i){
  try{
    const r = await fetch("/api/history/item?i="+i);
    BRIEF = await r.json(); PAST_INDEX = i;
    renderToday(); show("today");
    window.scrollTo({top:0, behavior:"smooth"});
  }catch(e){}
}
function backToLive(){
  PAST_INDEX = -1;
  if(LIVE_BRIEF){ BRIEF = LIVE_BRIEF; renderToday(); }
  else load(false);
}

// ————— CSV —————
function exportCSV(){
  const b = BRIEF; if(!b) return;
  const esc2 = s => '"'+String(s??"").replace(/"/g,'""')+'"';
  const rows = [["Date","Section","Platform/Client","Item","Detail"]];
  for(const [p,arr] of Object.entries(b.hashtags)) arr.forEach(t=>rows.push([b.date,"Hashtag",p,t.tag,t.note]));
  b.trends_in.concat(b.trends_global).forEach(t=>rows.push([b.date,"Trend",t.region,t.title,t.context]));
  [].concat(...Object.values(b.news)).forEach(n=>rows.push([b.date,"News",n.category,n.headline,n.source]));
  (b.brand||[]).forEach(br=>{
    rows.push([b.date,"Brand keywords",br.client,br.niche,(br.keywords||[]).map(k=>k.kw).join(", ")]);
    rows.push([b.date,"Brand hashtags",br.client,br.niche,(br.hashtags||[]).map(t=>t.tag).join(" ")]);
  });
  (NICHE||[]).forEach(m=>{
    rows.push([b.date,"Playbook",m.client,m.niche,m.idea+" | "+m.hashtags.join(" ")]);
    (m.posts||[]).forEach(([t,d])=>rows.push([b.date,"Post idea",m.client,t,d]));
    (m.reels||[]).forEach(([t,d])=>rows.push([b.date,"Reel idea",m.client,t,d]));
  });
  const blob = new Blob([rows.map(r=>r.map(esc2).join(",")).join("\n")], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "trend-brief-"+new Date().toISOString().slice(0,10)+".csv";
  a.click();
}

// ————— CLIENTS —————
function renderClients(){
  const list = clients();
  document.getElementById("clientList").innerHTML = list.length===0 ?
    '<div class="muted" style="text-align:center;padding:12px">No clients yet.</div>' :
    list.map((c,i)=>'<div class="panel row"><div><div class="pname">'+esc(c.name)+'</div>'+
      '<div class="muted small">'+esc([c.industry,c.keywords].filter(Boolean).join(" · "))+'</div></div>'+
      '<button class="btn small" style="color:var(--coral)" onclick="removeClient('+i+')">Remove</button></div>').join("");
}
function prefill(name, industry, keywords){
  document.getElementById("cName").value = name;
  document.getElementById("cIndustry").value = industry;
  document.getElementById("cKeywords").value = keywords;
}
async function addClient(){
  const name = document.getElementById("cName").value.trim(); if(!name) return;
  try{
    const r = await fetch("/api/clients/add", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({name, industry:document.getElementById("cIndustry").value.trim(),
                            keywords:document.getElementById("cKeywords").value.trim()})});
    CLIENTS = await r.json();
  }catch(e){}
  ["cName","cIndustry","cKeywords"].forEach(id=>document.getElementById(id).value="");
  renderClients(); NICHE=null; loadNiche(false);
  if(BRIEF) renderToday();
}
async function removeClient(i){
  try{
    const r = await fetch("/api/clients/remove", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({index:i})});
    CLIENTS = await r.json();
  }catch(e){}
  renderClients(); NICHE=null; loadNiche(false);
  if(BRIEF) renderToday();
}

load(false);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
