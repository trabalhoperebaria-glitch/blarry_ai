from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3, random, re
from colorama import Fore, init

init(autoreset=True)
app = Flask(__name__, static_folder="static")
CORS(app)
DB_PATH = "blarry.db"
conversations = {}

def db_connect():
    return sqlite3.connect(DB_PATH)

def normalize(text):
    return re.sub(r'[^\w\s]', '', (text or "").lower())

def search_by_game(game, limit=500):
    conn=db_connect(); c=conn.cursor()
    c.execute("SELECT response FROM responses WHERE game=? LIMIT ?", (game,limit))
    rows=[r[0] for r in c.fetchall()]; conn.close()
    return rows

def search_fts(message, limit=200):
    conn=db_connect(); c=conn.cursor(); results=[]
    try:
        words=[w for w in normalize(message).split() if len(w)>2]
        if not words: return []
        match_q=" OR ".join(words[:8])
        c.execute("SELECT response FROM responses_fts WHERE responses_fts MATCH ? LIMIT ?", (match_q,limit))
        results=[r[0] for r in c.fetchall()]
    except: pass
    finally: conn.close()
    return results

def get_random_sample(limit=100):
    conn=db_connect(); c=conn.cursor(); c.execute("SELECT response FROM responses ORDER BY RANDOM() LIMIT ?", (limit,))
    rows=[r[0] for r in c.fetchall()]; conn.close(); return rows

def blarry_ai_response(user_id,message):
    if user_id not in conversations: conversations[user_id]=[]
    conversations[user_id].append(message)
    msg_clean=normalize(message); words=msg_clean.split()
    last_reply=conversations[user_id][-1] if len(conversations[user_id])>1 else None
    found_game=None
    for w in words:
        if search_by_game(w,1):
            found_game=w; break
    responses=[]
    strategy=""
    if found_game: responses=search_by_game(found_game,500); strategy=f"game_exact({found_game})"
    if not responses: responses=search_fts(message,300); strategy="fts"
    if not responses and words:
        for w in words[:5]:
            res_w=search_by_game(w,200)
            if res_w: responses.extend(res_w); strategy="keywords_individual"
    if not responses: responses=get_random_sample(200); strategy="random_sample"
    reply=random.choice(responses) if responses else random.choice([
        "Desculpa, não achei algo específico. Pode dizer o nome do jogo ou explicar melhor?",
        "Não encontrei uma resposta direta — quer que eu dê uma dica geral?",
        "Ainda não sei sobre isso. Quer me dizer qual jogo ou categoria?"
    ])
    while reply==last_reply and len(responses)>1: reply=random.choice(responses)
    conversations[user_id].append(reply)
    print(Fore.CYAN+f"[Blarry AI] Estratégia:{strategy} | resultados:{len(responses)} | reply:{reply[:70]}")
    return reply

@app.route('/message', methods=['POST'])
def message():
    data=request.json or {}
    user_id=data.get('user_id',request.remote_addr)
    reply=blarry_ai_response(user_id,data.get('message',''))
    return jsonify({'reply':reply})

@app.route('/')
def home(): return send_from_directory('static','index.html')

if __name__=="__main__":
    print(Fore.YELLOW+"Servidor Blarry AI iniciado em http://0.0.0.0:5000")
    app.run(host='0.0.0.0',port=5000)
