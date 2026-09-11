# -*- coding: utf-8 -*-
"""
probe_api_chat.py — 測試 /api/chat/ 這個「有專屬 images 欄位」的端點

背景
----
probe_image_methods.py 測完 /api/flow_chat/ 的結論：完全不支援圖片，
10 種塞法全部失敗（有一種還會捏造假公文）。

後來找到一段程式碼，打的是另一個端點：

    POST https://pensieve.kh.asegroup.com/api/chat/
    {
        "token": token,
        "question": prompt,
        "chatHistory": [],
        "images": [{"img": "data:image/png;base64,..."}, ...],
        "empno": empno
    }

跟 flow_chat 的關鍵差異：
  * 圖片有專屬欄位 `images`（一個 list，每個元素是 {"img": data_uri}），
    不是塞進文字欄位裡讓模型自己猜——這是我們 10 種方法都沒測到的形狀。
  * 用 `question` 而不是 `variables.input`。
  * 有 `chatHistory`，看起來原生支援多輪對話（flow_chat 不吃歷史）。
  * 看起來不像綁定在特定 flow 上（flow_chat 的 token 裡編碼了 flow_id）。

★ 這支腳本只是「照著結構送一次真的圖，看回應像不像真的讀到內容」——
   不是官方保證。如果這段程式碼的來源不是官方文件，那這裡測出來的結果
   才是唯一能證明它有沒有用的方法。

★ 這裡用的 token 預設沿用 officialdoc_pipeline.py 裡 flow_chat 那組
   （PENSIEVE_TOKEN）。如果 /api/chat/ 其實要另一種 token（例如 Pensieve
   個人設定頁面裡的 Personal Access Token，而不是綁 flow 的自動化
   token），這裡打下去大概率會收到認證錯誤——用 --token 帶另一組再試。

用法：
    python probe_api_chat.py
    python probe_api_chat.py --pdf "另一份.pdf" --max-pages 2
    python probe_api_chat.py --token "<另一組 token>"
    python probe_api_chat.py --question "請問這份公文的來文字號跟主旨是什麼？"
"""

import argparse
import base64
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import officialdoc_pipeline as base
except ImportError:
    base = None

API_CHAT_URL = "https://pensieve.kh.asegroup.com/api/chat/"

DEFAULT_EMPNO = getattr(base, "PENSIEVE_EMPNO", "K20076")
VERIFY_SSL = getattr(base, "VERIFY_SSL", False)
PROXIES = getattr(base, "PROXIES", {"http": None, "https": None})
DEFAULT_PDF = (
    r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\2026"
    r"\1150728_1158090772_台灣電力股份有限公司高雄區營業處_K3B_(Security C).pdf"
)
DEFAULT_QUESTION = (
    "請仔細看這份掃描公文的圖片內容，回答：\n"
    "1. 來文字號是什麼？\n"
    "2. 主旨是什麼？\n"
    "3. 發文單位是什麼？\n"
    "如果圖片裡看不到某項資訊，明確說「看不到」，不要用附件檔名或猜測回答。"
)

if VERIFY_SSL is False:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_token(cli_token):
    if cli_token:
        return cli_token.strip()
    if base is not None:
        try:
            return base.get_token()
        except Exception:
            pass
    token = os.environ.get("PENSIEVE_TOKEN")
    if token:
        return token.strip()
    raise RuntimeError(
        "找不到 token。用 --token 直接帶入，或設環境變數 PENSIEVE_TOKEN，"
        "或確定 officialdoc_pipeline.py 在同一個資料夾且已填好 PENSIEVE_TOKEN。"
    )


def render_pdf_pages(pdf_path, max_pages, dpi):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("沒有安裝 PyMuPDF。請先執行：pip install PyMuPDF")

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"讀不到 PDF：{pdf_path}")

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages = []
    with fitz.open(pdf_path) as doc:
        n = min(max_pages, doc.page_count)
        for i in range(n):
            pix = doc[i].get_pixmap(matrix=matrix)
            pages.append(pix.tobytes("png"))
    return pages


def build_image_list(page_bytes_list):
    """完全照抓到的那段程式碼的結構：每個元素是 {"img": data_uri}。"""
    image_list = []
    for png_bytes in page_bytes_list:
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        image_list.append({"img": f"data:image/png;base64,{b64}"})
    return image_list


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--empno", default=DEFAULT_EMPNO)
    ap.add_argument("--token", default=None)
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--max-pages", type=int, default=1)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = get_token(args.token)

    print(f"轉圖：{args.pdf}（前 {args.max_pages} 頁 @ {args.dpi} dpi）…")
    page_bytes_list = render_pdf_pages(args.pdf, args.max_pages, args.dpi)
    for i, p in enumerate(page_bytes_list, 1):
        print(f"  第 {i} 頁 PNG → {len(p)} bytes")

    image_list = build_image_list(page_bytes_list)

    payload = {
        "token": token,
        "question": args.question,
        "chatHistory": [],
        "images": image_list,
        "empno": args.empno,
    }

    preview = json.loads(json.dumps(payload, ensure_ascii=False))
    preview["token"] = "***"
    preview["images"] = [f"<data URI, {len(img['img'])} 字元>" for img in preview["images"]]
    print("─" * 60)
    print(f"POST {API_CHAT_URL}")
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    print("─" * 60)

    if args.dry_run:
        print("（--dry-run：不實際送出）")
        return 0

    try:
        resp = requests.post(API_CHAT_URL, json=payload, timeout=args.timeout,
                             verify=VERIFY_SSL, proxies=PROXIES)
    except requests.RequestException as e:
        print(f"❌ 連線失敗：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"HTTP {resp.status_code} | Content-Type: {resp.headers.get('Content-Type')}")
    print("─" * 60)
    body_text = resp.content.decode("utf-8", errors="replace")
    try:
        print(json.dumps(json.loads(body_text), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(body_text[:3000])
    print("─" * 60)

    print("怎麼判斷有沒有真的讀到圖：")
    print("  * 回答裡的來文字號/主旨/發文單位，是不是跟真正的公文內容吻合")
    print("    （這份測試檔是台電高雄區營業處的來文，字號 1158090772）")
    print("  * 如果回答含糊、亂編，或直接說看不到——代表這條路一樣沒用")
    print("  * 如果 HTTP 4xx/5xx 且訊息像是認證錯誤——很可能是 token 類型不對，")
    print("    去 Pensieve 個人設定頁面找找看有沒有獨立的 API Token 可以用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
