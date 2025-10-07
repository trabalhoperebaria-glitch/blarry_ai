# server.py (agregador paralelo - versão mista)
import os, sqlite3, re, time, random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_ROOT, "blarry.db")
RAWG_KEY = os.getenv("RAWG_API_KEY", None)   # opcional
API_KEY = os.getenv("BLARRY_API_KEY", None)
WIKI_TTL_HOURS = 24

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

conversations = {}

# --- SQLite helpers (fallback local) ---
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = lambda cur, row: [row[i] for i in range(len(row))]
    return conn

def ensure_tables():
    conn = db_connect(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS wiki_cache (page TEXT PRIMARY KEY, extract TEXT, fetched_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS responses (id INTEGER PRIMARY KEY, game TEXT, keywords TEXT, response TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_game ON responses(game)")
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS responses_fts USING fts5(response, keywords, content='responses', content_rowid='id')")
    except:
        pass
    conn.commit(); conn.close()

ensure_tables()

def normalize(text):
    return re.sub(r"[^\w\s]", "", (text or "").lower())

def get_local_response(query):
    # tenta FTS
    conn = db_connect(); c = conn.cursor()
    try:
        words = [w for w in normalize(query).split() if len(w)>2]
        if words:
            match = " OR ".join(words[:8])
            c.execute("SELECT response FROM responses_fts WHERE responses_fts MATCH ? LIMIT 10", (match,))
            rows = [r[0] for r in c.fetchall()]
            if rows:
                return random.choice(rows)
    except:
        pass
    # fallback simple LIKE on keywords
    try:
        words = [w for w in normalize(query).split() if w]
        if words:
            clauses = " OR ".join(["keywords LIKE ?"]*len(words))
            params = [f"%{w}%" for w in words] + [10]
            q = f"SELECT response FROM responses WHERE {clauses} LIMIT ?"
            c.execute(q, params)
            rows=[r[0] for r in c.fetchall()]
            if rows: return random.choice(rows)
    except:
        pass
    # ultimate random
    try:
        c.execute("SELECT response FROM responses ORDER BY RANDOM() LIMIT 1")
        r=c.fetchone()
        return r[0] if r else None
    finally:
        conn.close()

# --- Source fetchers ---
def fetch_wikipedia(title):
    if not title: return None
    page = title.strip().replace(" ", "_")
    conn = db_connect(); c = conn.cursor()
    try:
        c.execute("SELECT extract, fetched_at FROM wiki_cache WHERE page=?", (page,))
        row = c.fetchone()
        if row:
            extract, fetched_at = row[0], row[1]
            if extracted_is_fresh(fetched_at):
                return extract
        url = f"https://pt.wikipedia.org/api/rest_v1/page/summary/{page}"
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            extract = data.get("extract")
            if extract:
                ts = int(time.time())
                try:
                    c.execute("INSERT OR REPLACE INTO wiki_cache(page, extract, fetched_at) VALUES (?,?,?)", (page, extract, ts))
                    conn.commit()
                except:
                    pass
                return extract
        return None
    except:
        return None
    finally:
        try: conn.close()
        except: pass

def extracted_is_fresh(ts):
    if not ts: return False
    fetched = datetime.fromtimestamp(int(ts))
    return datetime.utcnow() - fetched < timedelta(hours=WIKI_TTL_HOURS)

def fetch_rawg(query):
    if not RAWG_KEY: return None
    try:
        q = requests.get("https://api.rawg.io/api/games", params={"key":RAWG_KEY, "search":query, "page_size":3}, timeout=6)
        if q.status_code==200:
            j=q.json()
            results=[]
            for g in j.get("results",[]):
                name=g.get("name")
                desc=g.get("slug")
                # RAWG doesn't return long summary in free endpoint; craft short text
                results.append(f"{name}: jogo disponível (slug: {desc}).")
            return "\n".join(results) if results else None
    except:
        return None

def fetch_steam_store(query):
    # Steam store search scraping (simple)
    try:
        s = requests.get(f"https://store.steampowered.com/search/?term={requests.utils.quote(query)}", timeout=6)
        if s.status_code==200:
            soup = BeautifulSoup(s.text, "lxml")
            first = soup.select_one(".search_result_row")
            if first:
                title = first.get("data-ds-appid") or first.select_one(".title").get_text(strip=True)
                price = first.select_one(".search_price") and first.select_one(".search_price").get_text(strip=True)
                return f"Steam - {title}. Preço/extras: {price or 'N/A'}"
    except:
        return None

def fetch_generic_scrape(query):
    # Faz uma busca no DuckDuckGo HTML (sem JS) e pega snippets (respeite robots)
    try:
        hdr = {"User-Agent":"Mozilla/5.0"}
        s = requests.get(f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}", headers=hdr, timeout=6)
        if s.status_code==200:
            soup = BeautifulSoup(s.text, "lxml")
            res = soup.select(".result__snippet")
            if res:
                snippets = [r.get_text(strip=True) for r in res[:3]]
                return " ".join(snippets)
    except:
        return None

# aggregator: run multiple fetchers in parallel
def aggregate_sources(query, mode='casual'):
    fetchers = [fetch_wikipedia, fetch_generic_scrape, fetch_steam_store]
    if RAWG_KEY:
        fetchers.insert(1, fetch_rawg)
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = { ex.submit(f, query): f.__name__ for f in fetchers }
        for fut in as_completed(futures, timeout=8):
            try:
                txt = fut.result()
                if txt:
                    results.append((futures[fut], txt))
            except Exception:
                pass
    return results

# ---------------- main logic ----------------
def blarry_response(user_id, message, mode='casual'):
    if not message: return "Diz algo :)"
    if user_id not in conversations: conversations[user_id]=[]
    conversations[user_id].append(message)

    # Mode logic:
    reply = None
    # if gaming: prefer local DB
    if mode == 'gaming':
        local = get_local_response(message)
        if local:
            return local
        # try aggregator after local fails
        aggregated = aggregate_sources(message, mode)
        if aggregated:
            return aggregated[0][1]
        return "Não achei info específica; quer uma dica geral?"
    else:
        # casual: aggregator first
        agg = aggregate_sources(message, mode)
        if agg:
            # pick longest snippet / prioritize wikipedia
            for src, text in agg:
                if src == 'fetch_wikipedia':
                    return text
            # otherwise longest
            best = max(agg, key=lambda x: len(x[1]))[1]
            return best
        # fallback to local
        local = get_local_response(message)
        if local:
            return local
        return "Ainda não sei sobre isso — quer que eu pesquise outro jogo?"

# --- routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    q = data.get("question") or data.get("message") or ""
    mode = data.get("mode") or "casual"
    mode = "gaming" if str(mode).lower().startswith("g") else "casual"
    user_id = data.get("user_id") or request.remote_addr
    ans = blarry_response(user_id, q, mode=mode)
    return jsonify({"answer": ans, "mode": mode})

@app.route("/health")
def health():
    return jsonify({"status":"ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Blarry AI rodando em 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)

