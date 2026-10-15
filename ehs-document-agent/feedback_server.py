# -*- coding: utf-8 -*-
"""
公文 comment 回饋收集器——跑在 K20076 這台電腦上

officialdoc.html 是放在 \\\\10.10.51.67 的靜態網頁，瀏覽器沒辦法直接寫檔到
你電腦的 D:\\。所以在你電腦上開一個很小的 HTTP 服務，網頁上按「送出回饋」
就 POST 到這裡，這裡再寫進 knowledge\\feedback.jsonl。

  POST /feedback          收一筆回饋（分數 1~5；低於 5 分必須有原因）
  GET  /feedback/latest   每篇公文最新一筆回饋（網頁用來顯示「已回饋」）
  GET  /health            檢查服務是否活著

只用 Python 標準函式庫，不需要 pip install 任何東西。

執行：
    python feedback_server.py
    python feedback_server.py --port 8765

別台電腦要能連進來，需要開 Windows 防火牆（系統管理員身分執行一次）：
    netsh advfirewall firewall add rule name="officialdoc feedback" dir=in action=allow protocol=TCP localport=8765
"""

import argparse
import json
import logging
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import feedback_knowledge as fk


# ═══════════════════════════════════════════════════════════
# 設定區
# ═══════════════════════════════════════════════════════════

HOST = "0.0.0.0"          # 0.0.0.0 = 讓別台電腦也連得到；只給自己用就改 127.0.0.1
PORT = 8765

# 允許哪個網頁來源呼叫。內網用 "*" 最省事；
# 想鎖死就填網頁的來源，例如 "http://10.10.51.67"
ALLOWED_ORIGIN = "*"

MAX_BODY_BYTES = 64 * 1024     # 單筆回饋的大小上限
MAX_FIELD_CHARS = 4000         # 每個文字欄位最多存幾個字

LOG_FILE = os.path.join(fk.KNOWLEDGE_DIR, "feedback_server.log")

# 只存這些欄位。不記評分人、時間、來源 IP
TEXT_FIELDS = ("來文字號", "主旨", "summary", "指派人員", "知會人員", "comment",
               "reason", "suggested_comment")


# ═══════════════════════════════════════════════════════════

log = logging.getLogger("feedback")
_write_lock = threading.Lock()


def setup_logging():
    os.makedirs(fk.KNOWLEDGE_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # 用 pythonw / 工作排程器在背景跑時沒有 console，sys.stdout 會是 None
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def validate(data):
    """回傳 (清理過的紀錄, 錯誤訊息)。錯誤訊息會直接顯示在網頁上。"""
    if not isinstance(data, dict):
        return None, "格式錯誤：需要 JSON 物件"

    score = data.get("score")
    if isinstance(score, str) and score.strip().isdigit():
        score = int(score.strip())
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        return None, "分數必須是 1~5 的整數"

    rec = {}
    for field in TEXT_FIELDS:
        value = data.get(field)
        rec[field] = "" if value is None else str(value).strip()[:MAX_FIELD_CHARS]

    if score < 5 and not rec["reason"]:
        return None, "低於 5 分請說明原因"
    if not rec["comment"]:
        return None, "缺少被評分的 comment"
    if not fk.doc_key(rec):
        return None, "缺少來文字號與主旨，無法辨識是哪一篇公文"

    rec["score"] = score
    return rec, None


def append_feedback(rec):
    """一筆一行附加到 feedback.jsonl。上鎖避免兩個人同時送出時寫在同一行。"""
    os.makedirs(os.path.dirname(fk.FEEDBACK_FILE), exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False)
    with _write_lock:
        with open(fk.FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


class Handler(BaseHTTPRequestHandler):
    server_version = "OfficialDocFeedback/1.0"

    # ── 回應工具 ──────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome 的 Private Network Access：內網網頁呼叫另一台內網機器時會先問這個
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 預設的存取紀錄會帶來源 IP，這裡只記「哪個請求、回了什麼狀態」，查問題用
        log.info("%s", fmt % args)

    # ── 路由 ──────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/health":
            return self._json(200, {"ok": True, "file": fk.FEEDBACK_FILE})

        if path == "/feedback/latest":
            latest = fk.latest_by_doc(fk.load_feedback())
            items = {
                key: {k: r.get(k, "") for k in
                      ("score", "reason", "suggested_comment", "comment")}
                for key, r in latest.items()
            }
            return self._json(200, {"ok": True, "items": items})

        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/feedback":
            return self._json(404, {"ok": False, "error": "not found"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._json(400, {"ok": False, "error": "回饋內容是空的或太大"})

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "不是合法的 JSON"})

        rec, error = validate(data)
        if error:
            log.warning("拒收：%s", error)
            return self._json(400, {"ok": False, "error": error})

        # id 只是給 pipeline 判斷「這筆回饋重產過了沒」用
        rec = {"id": uuid.uuid4().hex, **rec}

        try:
            append_feedback(rec)
        except OSError as e:
            log.error("寫入失敗：%s: %s", type(e).__name__, e)
            return self._json(500, {"ok": False, "error": f"寫入失敗：{e}"})

        log.info("收到回饋 %s 分｜%s｜%s", rec["score"], fk.doc_key(rec), rec["reason"][:40])
        self._json(200, {"ok": True, "id": rec["id"]})


def main():
    parser = argparse.ArgumentParser(description="公文 comment 回饋收集器")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    setup_logging()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("回饋服務啟動 http://%s:%d ，寫入 %s", args.host, args.port, fk.FEEDBACK_FILE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中斷，關閉服務")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
