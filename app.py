# app.py
import os
import threading
import time
from flask import Flask, jsonify
from bot import start_bot_background  # hàm bạn cần tạo trong bot.py

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot running"

# health check endpoint
@app.route("/health")
def health():
    return jsonify({"status":"ok"})

# start bot in background thread once when app starts
def start_bot_thread():
    try:
        start_bot_background()  # hàm này start polling / scheduler, non-blocking
    except Exception as e:
        print("Error starting bot:", e)

# ensure only start once
_thread_started = False
def ensure_bot_started():
    global _thread_started
    if not _thread_started:
        t = threading.Thread(target=start_bot_thread, daemon=True)
        t.start()
        _thread_started = True

# Gunicorn will import app; call ensure_bot_started on import
ensure_bot_started()
