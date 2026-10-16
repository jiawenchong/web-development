# 公文簽核 agent — Pensieve 圖片/掃描檔傳輸測試

## 這個資料夾放什麼

| 檔案 | 用途 |
|---|---|
| `officialdoc_pipeline.py` | 主要 pipeline（下載報表 → 比對 PDF → 呼叫 Pensieve → 寫 `officialdoc.json`）。這就是你要求「換回來」的那個版本，內容逐字保留。 |
| `probe_image_methods.py` | 新增的測試工具：把「怎麼把圖片/掃描檔傳給 Pensieve」拆成 10 種常見做法，逐一送出去讓你自己比對哪一種真的有效。 |

## ⚠️ 先說清楚我能做到哪裡

`pensieve.kh.asegroup.com` 是你們公司內網站台，`U:\...` 是你 Windows 電腦上
掛載的網路磁碟。我（Claude）現在這個對話跑在 Anthropic 的雲端沙箱容器裡，
**兩者都連不到、讀不到**。所以：

* 我沒辦法真的幫你打這支 API 去驗證哪個方法有效。
* 我沒辦法真的讀取那份 `1150728_1158090772_台灣電力股份有限公司高雄區營業處_
  K3B_(Security C).pdf`。

`probe_image_methods.py` 是我能做到的部分：把 10 種候選做法寫成一支
**可以在你自己那台連得到內網又掛得到 U:\ 的電腦上執行**的腳本，跑完會把
每一種方法的 HTTP 狀態碼、回應內容存成報告，你（或貼回來給我）就能判斷
哪個真的行得通。

官方文件只寫了 `POST /api/flow_chat/`（`token` / `empno` / `variables`）這種
純文字 JSON 呼叫方式，完全沒提檔案/圖片怎麼傳——所以這 10 種本質上都是
「合理猜測」，不是「官方保證可行」。

## 怎麼跑

在你那台機器（能連 `pensieve.kh.asegroup.com`、能讀 `U:\`）上：

```bash
pip install requests PyMuPDF
# 如果同一資料夾已經有填好 token 的 officialdoc_pipeline.py，probe 腳本會直接沿用

python probe_image_methods.py --list
```

會印出 10 種方法的說明。接著先用 `--dry-run` 看每種方法實際會送出什麼樣的
body（不會真的打 API）：

```bash
python probe_image_methods.py --dry-run
```

確認 body 長相沒問題後，正式送出（預設會測全部 10 種，每種中間停 3 秒）：

```bash
python probe_image_methods.py
```

只想測其中幾種：

```bash
python probe_image_methods.py --only 3,6,9
```

跑完會在同一資料夾產生 `probe_image_methods_report.json`，裡面是每種方法的
HTTP 狀態碼與回應內容（截斷到 2000 字）。

## 10 種方法在測什麼

1. **整份 PDF 的 base64** 直接當字串塞進 `input`。
2. **整份 PDF 的 data URI**（`data:application/pdf;base64,...`）塞進 `input`。
3. **第一頁轉成 PNG 的 data URI** 塞進 `input`（掃描公文本來就是整頁一張圖，
   用 PyMuPDF 直接 render 頁面，不是抽文字）。
4. `input` 放一段 **JSON 字串**，內含 `filename` / `mime_type` /
   `content_base64`（模仿常見的「檔案物件」慣例，跟 pipeline 現有的
   `PAYLOAD_AS_STRING=True` 邏輯一致）。
5. `input` 放 **「每頁一張圖」的 JSON 陣列字串**。
6. `input` 照舊放文字說明，**另開一個 `image` 變數**放 base64（文字 + 附件
   分開兩個 variables）。
7. 跟 6 類似，但變數名稱換成 **`attachment`**，值用 data URI。
8. **Markdown 內嵌圖片語法**寫進 `input` 的文字裡（`![](data:image/png;...)`）。
9. 改用 **`multipart/form-data`**，`token` / `empno` / `variables` 當表單欄位，
   PDF 檔案本體用 `files=` 欄位名稱 `file` 上傳。
10. 跟 9 類似，但欄位名稱換成 `image`，且只上傳**第一頁的 PNG**（防止後端
    限定只收圖片 mime type、拒收 PDF）。

## 怎麼判讀結果

* **HTTP 4xx** → 這個方法的欄位名稱/格式，Pensieve 直接拒收。
* **HTTP 200，但回應像是完全沒看懂附件**（例如把 base64 當亂碼文字複誦回來，
  或直接說看不到附件）→ 平台收到了，但沒有把它當「圖」處理。
* **HTTP 200，且回應內容真的提到圖片裡的資訊**（例如複誦了公文上的文字、
  發文字號、印章內容）→ 這個方法可行，可以正式採用。

## 如果 10 種全部都不行

10 種都是合理猜測，不保證涵蓋官方實際實作。最準確、一次到位的做法是：

1. 打開瀏覽器進 Pensieve 網頁版聊天室，找到同一個 flow。
2. 打開瀏覽器 DevTools（F12）→ Network 分頁。
3. 手動上傳一張圖片給這個 flow（就是你說「手動 OK」的那個操作）。
4. 在 Network 分頁裡找到那個上傳的請求，看：
   * 打到哪個 URL（可能不是 `/api/flow_chat/`，而是先打一個
     `/api/upload/` 之類的端點拿到檔案 ID/URL，之後才在 `flow_chat`
     的 `variables` 裡帶那個 ID/URL 進去 —— 這是很多聊天平台的標準做法）
   * Content-Type 是 `application/json` 還是 `multipart/form-data`
   * Request Payload 的欄位名稱長什麼樣

把那個請求的截圖或「Copy as cURL」貼給我，我可以直接照著改
`officialdoc_pipeline.py`，不用再猜。

## 實測結果（2026-09-11，用 K3B 這份公文測完 10 種方法）

10 種方法 HTTP 都回 200，但**沒有一種真的讀到圖片內容**：

* **方法 9、10（multipart）** → 直接 `{"isSuccess": false, "Result": "Service internal error."}`。
  這是後端真的嘗試處理後出錯，不是靜默忽略——證實 `/api/flow_chat/`
  **完全不接受 multipart/form-data**，只吃 JSON。
* **方法 3、5**（明確帶 `data:image/...` 或圖片陣列）→ 誠實回覆
  「本件為圖片格式公文，無法擷取全文」「系統無法自動提取全文」。
  沒讀到，但至少沒有亂編，算是「乾淨的失敗」。
* **方法 4、6、7、8** → 回來的字號/主旨，都只是你自己寫在 `input` 裡
  「看得懂的文字部分」被抓出來重複，那一大串 base64 完全沒被用到。
* **方法 1**（整份 PDF 的 base64，旁邊沒有任何可讀文字線索）→ **最危險的
  結果**：編出一份完全不存在的假公文（「來文字號 RSPL-1.0」「主旨：半導體
  製程設備維護與升級計畫」），跟真正的台電 K3B 公文毫無關係，還配了假的
  指派人員與知會人員。這比「查不到」嚴重得多——是自信地生成一份看起來
  完全合理、實際上是幻覺出來的審核結果，混進正式流程不會有人發現。

**結論：`flow_chat` 背後是純文字 LLM，沒有視覺/OCR 能力，也不接受檔案
上傳。不是格式沒猜對，是這條路線本身走不通。**

## 找到真正的上傳端點：`/mindMate/upload_file/`

從瀏覽器 DevTools 抓到 Pensieve 網頁版手動上傳時打的真實請求：

```
POST https://pensieve.kh.asegroup.com/mindMate/upload_file/
Content-Type: multipart/form-data
Cookie: PensieveLog=...; sessionid=...
Referer: https://pensieve.kh.asegroup.com/flow/5d5d65ad19374419b281ecab12114ca9
```

跟 `flow_chat` 的關鍵差異：

* **命名空間不同**：`mindMate`（網頁後台）vs `api`（flow_chat 的 REST API）。
* **驗證方式不同**：這裡用瀏覽器登入後的 **session cookie**
  （`sessionid` / `PensieveLog`），不是 flow_chat 用的 `token` + `empno`。
* **綁定 flow**：Referer 帶的 flow_id 跟 flow_chat token 裡的 flow_id
  完全一致，代表上傳綁在你正在開的那個 flow 頁面底下。
* 欄位名稱是 `file`（跟我們猜的方法 9 一樣，只是端點打錯了）。

新增了 `probe_mindmate_upload.py`，可以直接用抓到的 session cookie
測試真的上傳一份 PDF 上去，把伺服器回應的內容印出來。

**還缺的兩塊拼圖**（需要再回瀏覽器 DevTools 抓一次）：

1. `upload_file` 這個請求的 **Response**（上傳完伺服器回了什麼——
   多半是一個 file id 或 url）。
2. 上傳完、送出對話之後，**緊接著送出去的下一個請求**——這個請求的
   body 裡怎麼引用剛剛上傳的檔案，這是唯一能確定「flow_chat 端 API
   要怎麼帶檔案代號進去」的方法。

另外需要確認：這個 `mindMate/upload_file/` 用的是 session cookie
驗證，flow_chat 用的是 token + empno；上傳完的檔案，token 那邊呼叫
flow_chat 時到底認不認得到——這點還沒驗證，如果認不到，代表檔案上傳
本質上只能透過「已登入的瀏覽器 session」完成，你現有的 token-based
自動化腳本可能連不上這條路，需要跟平台管理者確認是否有對應的
API-key 版本上傳端點。

⚠️ `sessionid` / `PensieveLog` 這組 cookie 等同你的登入身分，**不要
寫進程式碼、不要 commit 進 git**，用 `probe_mindmate_upload.py` 時一律
用命令列參數或環境變數帶入，測完就丟。

## 目前已知：K3B 這份公文在既有 pipeline 裡會怎樣

`officialdoc_pipeline.py` 目前用 `pdfplumber` 的 `extract_text()` 抽文字。
掃描檔（整頁是圖、沒有文字層）用這個方法一定抽到空字串，程式就會把它標成
`"紙本公文，無法解析"`。這跟本次測試的圖片傳輸方式是**兩個獨立問題**：

* 如果最後 Pensieve 那邊確定「能吃圖」，等於可以把掃描檔直接送圖過去審核，
  完全繞過 `pdfplumber` 這條路。
* 如果 Pensieve 那邊其實**不吃圖、只吃文字**，那退而求其次的做法是在
  `officialdoc_pipeline.py` 本機端先做 **OCR**（例如 `pytesseract` +
  PyMuPDF render 出來的頁面圖），把辨識出來的文字塞進現有 `全文` 欄位，
  流程完全不用動 Pensieve 那端。這個做法不需要猜測 API 格式，是相對確定
  能動的備案 —— 如果 10 種都測不出結果，可以考慮改走這條。

---

## Comment 回饋機制（讓 LLM 從使用者評分學習）

程式與產出都在 **10.10.28.170**，不經過你的電腦。大家打開 **http://10.10.28.170:8765/** 看公文、評分。

### 10.10.28.170 資料夾結構

全部放在同一個資料夾 `D:\Web Service\official_doc`（`officialdoc_pipeline.py` 的 `BASE_DIR`）：

```
D:\Web Service\official_doc\
├─ officialdoc_pipeline.py          主程式（排程跑這支）
├─ download_report.py               Selenium 下載腳本（下載資料夾要是這個資料夾）
├─ feedback_server.py               網頁 + 收回饋服務
├─ feedback_knowledge.py            整理回饋、上傳知識庫
├─ start_feedback_server.bat        啟動網頁/回饋服務
├─ officialdoc.html                 網頁
├─ vector_id.txt                    知識庫 vector_id（單行）
├─ pdf\scan\                        ← STEP 0 從 U:\...\● 公文掃瞄\新增資料夾 複製過來
├─ pdf\download\                    ← STEP 0 從 U:\...\● 公文掃瞄\公文下載-自動化\解壓縮 複製過來
│  （以下自動產生）
├─ 環安衛收發文管理查詢報表_(Security B).xls
├─ officialdoc.json                 pipeline 結果，網頁讀這個
├─ feedback.jsonl                   所有回饋
├─ feedback_digest.md / officialdoc_feedback_cases.txt / .vector_upload_state.json
├─ pipeline.log / feedback_server.log / .review_cache.json / 未簽核的公文全文.json
```

網頁服務只提供 `officialdoc.html` 與 `officialdoc.json`，資料夾裡其他檔案（程式、token、vector_id、回饋）用瀏覽器都拿不到。

### 資料怎麼流

```
STEP 0  U: 共用資料夾的新 PDF → 複製到 pdf\scan、pdf\download（PDF_SYNC_SOURCE_ROOT）
STEP 1  download_report.py 下載環安衛報表
STEP 2  比對報表與 PDF 全文
STEP 3  每篇送 Pensieve；主旨相似的回饋放進「參考回饋」；被評低分的自動重產
STEP 4  寫 officialdoc.json → 網頁更新
之後    更新 feedback_digest.md、回饋有變就上傳知識庫（replace）

網頁評分 → POST /feedback → feedback.jsonl → 下次 pipeline 讀到
```

只存分數、原因、建議 comment 與當時的公文內容，不記評分人、時間、來源 IP。

### 第一次設定（在 10.10.28.170 上）

1. **PDF 來源**（兩種擇一）：
   * **A. 你的電腦推過去（.170 讀不到 U: 時用這個）**：在你的電腦放 `sync_pdfs_to_170.py`，
     先執行 `python sync_pdfs_to_170.py --save-password` 存 .170 的密碼（DPAPI 加密，只有你的帳號在你的電腦上解得開），
     再 `python sync_pdfs_to_170.py --dry-run` → `python sync_pdfs_to_170.py`。
     你的電腦工作排程設「只在使用者登入時執行」，時間比 .170 的 pipeline 早；`PDF_SYNC_SOURCE_ROOT` 保持空的。
   * **B. .170 自己去 U: 拉**：.170 要先能存取 U: 那台伺服器，步驟如下。
   * 在你的電腦 cmd 打 `net use`，找 U: 那一行，例如 `\\fs01\dept`。
   * 在 .170 存那台伺服器的帳密（用之後跑排程的帳號登入 .170 做一次）：
     `cmdkey /add:fs01 /user:fs01\administrator /pass`
   * 在 .170 檔案總管打開 `\\fs01\dept\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄`，不跳密碼視窗就 OK。
   * 填 `officialdoc_pipeline.py` 的 `PDF_SYNC_SOURCE_ROOT`（同一個路徑）。
2. **download_report.py**：確認下載資料夾是 `D:\Web Service\official_doc`。
3. **套件**：`pip install pandas pdfplumber requests xlrd lxml`；Chrome 與對應版本的 driver。
4. **防火牆** 8765 port（系統管理員 cmd）：
   `netsh advfirewall firewall add rule name="officialdoc web" dir=in action=allow protocol=TCP localport=8765`
5. **手動測試**（在 `D:\Web Service\official_doc` 執行）：
   * `start_feedback_server.bat` → 從你的電腦打開 `http://10.10.28.170:8765/`
   * `python officialdoc_pipeline.py --dry-run` → log 有「STEP 0 … 新複製 N 個」與「→ N 個 PDF」
   * `python officialdoc_pipeline.py --probe` → 連得到 Pensieve
   * `python officialdoc_pipeline.py` → 網頁出現公文
6. **工作排程器**兩個工作，開始位置都填 `D:\Web Service\official_doc`：
   * 網頁/回饋服務：觸發「電腦啟動時」，執行 `start_feedback_server.bat`；「設定」取消勾選「如果工作執行超過…停止」。
   * pipeline：照原本的時間，程式填 python 完整路徑（`where python`），引數 `officialdoc_pipeline.py`。
   兩個都用**做第 1 步 cmdkey 的同一個帳號**執行，否則讀不到 U: 的 PDF。
7. **Pensieve**：`prompt/comment.txt`、`prompt/summary.txt` 貼進 flow 並**發布**；comment node 讀得到原始 `input`；
   專用知識庫設 replace、掛到 comment 那個 LLM node。

### 日常怎麼用

* 打開 `http://10.10.28.170:8765/` 看公文、評分即可。
* 同一種錯一直出現（看 `feedback_digest.md`）→ 改 `comment.txt` 的規則，貼進 flow 發布，再把 pipeline 的 `PROMPT_VERSION` +1。
* 偶爾看 `pipeline.log` 有沒有 `[WARNING]` / `[ERROR]`（PDF 同步失敗、知識庫上傳失敗都不會讓 pipeline 停下來，只會留記錄）。
