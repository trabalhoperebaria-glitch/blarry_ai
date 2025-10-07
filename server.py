from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sqlite3

app = Flask(__name__)
CORS(app)

DB_PATH = "blarry.db"

# Página principal
@app.route("/")
def index():
    return render_template("index.html")

# Endpoint de mensagens
@app.route("/message", methods=["POST"])
def message():
    data = request.json
    user_message = data.get("message", "")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT response FROM responses ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    conn.close()

    reply = row[0] if row else "Desculpe, não sei o que responder."
    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run()
