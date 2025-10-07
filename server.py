# server.py
# Blarry AI - Modo Misto (Wikipedia + banco local grande)
# Usage: python server.py  (or use gunicorn server:app in production)
# Requires: Flask, Flask-Cors, requests, colorama
# Set BLARRY_API_KEY env var to protect API (optional but recommended)

import os
import sqlite3
import requests
import random
import re
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from colorama import Fore, init

init(autoreset=True)

# Config
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_ROOT, "blarry.db")
WIKI_CACHE_TTL_HOURS = 24  # tempo em horas para considerar cache da wiki válido
DEFAULT_MODE = "casual"  # 'casual' or 'gaming'
API_KEY = os.getenv("BLARRY_API_KEY", None)  # se setado, exigirá header X-API-Key

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

# memória simples por usuário (IP ou user_id)
conversations = {}  # { user_id: [msg1, reply1, msg2, reply2, ...] }


# --------------------
# Utilitários SQLite
# --------------------
def db_connect():
    # sqlite default is fine; enable row_factory for convenience
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = lambda cursor, row: [row[i] for i in range(len(row))]
    return conn

def ensure_meta_tables():
    """Cria tabelas auxiliares (cache wiki) e índices mínimos se não existirem."""
    conn = db_connect()
    c = conn.cursor()
    # tabela para cachear resultados da wikipedia
    c.execute("""
    CREATE TABLE IF NOT EXISTS wiki_cache (
        page TEXT PRIMARY KEY,
        extract TEXT,
        fetched_at INTEGER
    )
    """)
    # se não existir tabela responses, cria esqueleto (vazia) — não popula
    c.execute("""
    CREATE TABLE IF NOT EXISTS responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT,
        keywords TEXT,
        response TEXT
    )
    """)
    # índice para busca por game
    c.execute("CREATE INDEX IF NOT EXISTS idx_game ON responses(game)")
    # tenta criar FTS virtual se possível (silenciosamente)
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS responses_fts USING fts5(response, keywords, content='responses', content_rowid='id')")
    except Exception:
        # SQLite da plataforma pode não suportar FTS5; tudo bem
        pass
    conn.commit()
    conn.close()

ensure_meta_tables()


# --------------------
# Proteção por API Key
# --------------------
@app.before_request
def check_api_key():
    if API_KEY is None:
        return  # sem proteção
    # rotas públicas: index, static assets
    path = request.path or ""
    if path.startswith("/static") or path in ("/", "/health"):
        return
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401


# --------------------
# Helpers de busca
# --------------------
def normalize(text):
    return re.sub(r"[^\w\s]", "", (text or "").lower())

def fetch_wikipedia_summary(title):
    """Busca no cache, se não, consulta a Wikipedia REST e armazena no cache."""
    if not title:
        return None
    page = title.strip().replace(" ", "_")
    conn = db_connect()
    c = conn.cursor()
    # checar cache
    try:
        c.execute("SELECT extract, fetched_at FROM wiki_cache WHERE page = ?", (page,))
        row = c.fetchone()
        if row:
            extract, fetched_at = row[0], row[1]
            if extracted_is_fresh(fetched_at):
                conn.close()
                return extract
    except Exception:
        pass  # se erro no cache, continuamos para buscar na web

    # buscar na Wikipedia (português)
    url = f"https://pt.wikipedia.org/api/rest_v1/page/summary/{page}"
    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            extract = data.get("extract")
            if extract:
                # armazenar no cache
                try:
                    ts = int(time.time())
                    c.execute("INSERT OR REPLACE INTO wiki_cache(page, extract, fetched_at) VALUES (?,?,?)", (page, extract, ts))
                    conn.commit()
                except Exception:
                    pass
                conn.close()
                return extract
        elif resp.status_code == 404:
            conn.close()
            return None
    except Exception:
        # falha na requisição (timeout, rede)
        try:
            conn.close()
        except:
            pass
        return None
    try:
        conn.close()
    except:
        pass
    return None

def extracted_is_fresh(fetched_at_ts):
    if fetched_at_ts is None:
        return False
    fetched = datetime.fromtimestamp(int(fetched_at_ts))
    return datetime.utcnow() - fetched < timedelta(hours=WIKI_CACHE_TTL_HOURS)


def search_local_by_game(game, limit=500):
    """Busca respostas por game exato (coluna game)."""
    if not game:
        return []
    conn = db_connect()
    c = conn.cursor()
    try:
        c.execute("SELECT response FROM responses WHERE game = ? COLLATE NOCASE LIMIT ?", (game, limit))
        rows = [r[0] for r in c.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return rows

def search_local_fts(message, limit=200):
    """Busca por FTS se existir; caso contrário, faz LIKE por palavras-chave."""
    conn = db_connect()
    c = conn.cursor()
    results = []
    try:
        # tenta FTS5 primeiro
        try:
            words = [w for w in normalize(message).split() if len(w) > 2]
            if words:
                match_q = " OR ".join(words[:8])
                c.execute("SELECT response FROM responses_fts WHERE responses_fts MATCH ? LIMIT ?", (match_q, limit))
                results = [r[0] for r in c.fetchall()]
                if results:
                    conn.close()
                    return results
        except Exception:
            pass
        # fallback: LIKE em keywords (mais lento)
        words = [w for w in normalize(message).split() if w]
        if not words:
            conn.close()
            return []
        clauses = " OR ".join(["keywords LIKE ?"] * len(words))
        params = [f"%{w}%" for w in words] + [limit]
        q = f"SELECT response FROM responses WHERE {clauses} LIMIT ?"
        c.execute(q, params)
        results = [r[0] for r in c.fetchall()]
    except Exception:
        results = []
    conn.close()
    return results

def get_random_sample(limit=100):
    conn = db_connect()
    c = conn.cursor()
    try:
        c.execute("SELECT response FROM responses ORDER BY RANDOM() LIMIT ?", (limit,))
        rows = [r[0] for r in c.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return rows


# --------------------
# Lógica principal: mistura Wiki + local
# --------------------
def blarry_ai_response(user_id, message, mode=DEFAULT_MODE):
    """Retorna resposta: modo 'gaming' favorece banco local; 'casual' favorece Wikipedia."""
    if not message:
        return "Diz algo que eu possa responder 😊"

    # guarda na memória
    if user_id not in conversations:
        conversations[user_id] = []
    conversations[user_id].append(message)

    # limpar mensagem curta
    query = message.strip()

    reply = None
    strategy = None

    # Se modo gaming: primeiro local, depois wiki
    if mode == "gaming":
        # 1) procurar por nome de jogo explicitamente na frase (palavra exata)
        words = [w for w in normalize(query).split() if w]
        for w in words:
            local = search_local_by_game(w, limit=10)
            if local:
                reply = random.choice(local)
                strategy = f"local_game_exact({w})"
                break
        # 2) FTS local
        if not reply:
            results = search_local_fts(query, limit=200)
            if results:
                reply = random.choice(results)
                strategy = "local_fts"
        # 3) Wikipedia
        if not reply:
            wiki = fetch_wikipedia_summary(query)
            if wiki:
                reply = wiki
                strategy = "wikipedia"
    else:
        # modo casual/default: tenta wiki primeiro (mais natural), depois local
        wiki = fetch_wikipedia_summary(query)
        if wiki:
            reply = wiki
            strategy = "wikipedia"
        else:
            # tenta local por FTS
            results = search_local_fts(query, limit=300)
            if results:
                reply = random.choice(results)
                strategy = "local_fts"
            else:
                # tenta por game exato
                words = [w for w in normalize(query).split() if w]
                for w in words:
                    local = search_local_by_game(w, limit=10)
                    if local:
                        reply = random.choice(local)
                        strategy = f"local_game_exact({w})"
                        break

    # fallback aleatório
    if not reply:
        sample = get_random_sample(200)
        if sample:
            reply = random.choice(sample)
            strategy = "random_sample"
        else:
            reply = "Ainda não sei sobre isso. Pode tentar outro jogo ou palavra-chave."
            strategy = "empty_fallback"

    # evita repetição imediata com a última resposta do usuário
    last_msgs = conversations.get(user_id, [])
    # último elemento costuma ser a pergunta; penúltimo a resposta
    last_reply = None
    if len(last_msgs) >= 2:
        last_reply = last_msgs[-1]
    # prevenir repetir exatamente a mesma resposta em sequência
    tries = 0
    while reply == last_reply and tries < 6:
        # tentar outra resposta do pool
        pool = []
        # regenera pool dependendo da strategy
        if strategy and "local" in strategy:
            pool = search_local_fts(query, limit=200)
        elif strategy == "wikipedia":
            pool = [reply]
        else:
            pool = get_random_sample(200)
        if pool:
            reply = random.choice(pool)
        tries += 1

    conversations[user_id].append(reply)
    print(Fore.CYAN + f"[Blarry AI] user={user_id} mode={mode} strat={strategy} preview={reply[:90]}")
    return reply


# --------------------
# Rotas
# --------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": int(time.time())})

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    question = data.get("question") or data.get("message") or ""
    mode = data.get("mode") or DEFAULT_MODE
    user_id = data.get("user_id") or request.remote_addr
    # sanitize mode
    mode = "gaming" if str(mode).lower().startswith("g") else "casual"

    reply = blarry_ai_response(user_id, question, mode=mode)
    return jsonify({"answer": reply, "mode": mode})

# manter compatibilidade com rota antiga /message
@app.route("/message", methods=["POST"])
def message():
    data = request.json or {}
    msg = data.get("message", "")
    user_id = data.get("user_id") or request.remote_addr
    reply = blarry_ai_response(user_id, msg, mode=DEFAULT_MODE)
    return jsonify({"reply": reply})


# --------------------
# Run
# --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(Fore.YELLOW + f"🚀 Blarry AI (misto) iniciando em http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
