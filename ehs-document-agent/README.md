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

### 放在哪裡

| 位置 | 放什麼 | 怎麼跑 |
|---|---|---|
| **你的電腦** `D:\ASEKH\K20076\2026\公文簽核agent\` | `officialdoc_pipeline.py`、`download_report.py`、`pensieve_token.txt`、**`vector_id.txt`** | 照原本的排程跑，不用改 |
| **10.10.51.67** `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\` | `feedback_server.py`、`feedback_knowledge.py`、`start_feedback_server.bat`；回饋資料也產生在這裡 | 回饋服務開機自動啟動，一直開著 |
| **10.10.51.67** 網頁資料夾 | `officialdoc.html`、`data\officialdoc.json` | 跟原本一樣 |

pipeline 會直接從 `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc` 載入 `feedback_knowledge.py` 並讀寫那裡的回饋資料，
你電腦上不用再放一份。資料夾讀不到時只記警告，公文審核照常跑（只是這次不帶回饋）。

### 資料怎麼流

```
officialdoc.html（每張卡片 comment 下方：1~5 星，低於 5 分必填原因，可選填「建議的 comment」）
      │ POST http://10.10.51.67:8765/feedback   （網頁自動用自己的主機名稱，不用填 IP）
      ▼
feedback_server.py（在 .67）──寫入──> \\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\feedback.jsonl
      │
      ▼ 你電腦的 pipeline 下次排程跑時
  ① prompt：每篇公文挑主旨最像的 ≤3 筆回饋，放在 input 的「參考回饋」欄位送給 flow
  ② 快取裡被打低分的那篇，帶著修正意見自動重新產生一次
  ③ 知識庫：所有回饋寫成案例卡 officialdoc_feedback_cases.txt，用 /api/uploadVector/ 上傳
     （知識庫設定成 replace，同檔名＝覆蓋；內容沒變就不重傳）
  ④ 更新 feedback_digest.md 彙整檔，給人看、用來改 prompt 規則
```

只存分數、原因、建議 comment 與當時的公文內容，不記評分人、時間、來源 IP。

### \\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc 裡會出現的檔案

| 檔案 | 誰寫的 | 內容 |
|---|---|---|
| `feedback.jsonl` | 回饋服務 | 所有回饋，一筆一行。評錯了可以直接用記事本刪掉那一行 |
| `feedback_server.log` | 回饋服務 | 服務的執行紀錄 |
| `feedback_digest.md` | pipeline | 回饋彙整，給人看 |
| `officialdoc_feedback_cases.txt` | pipeline | 上傳到知識庫的案例卡 |
| `.vector_upload_state.json` | pipeline | 上次上傳的內容指紋，用來判斷要不要重傳 |

### 第一次設定（.67 只有資料夾權限、回饋服務跑在你電腦的做法）

1. 把 `feedback_server.py`、`feedback_knowledge.py`、`start_feedback_server.bat` 複製到 `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\`。
2. 你的電腦：換上新版 `officialdoc_pipeline.py`，把 vector_id 存成 `D:\ASEKH\K20076\2026\公文簽核agent\vector_id.txt`。
3. 你的電腦：雙擊 `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\start_feedback_server.bat`，黑色視窗要一直開著（關掉＝服務停止）。
   瀏覽器開 `http://127.0.0.1:8765/health`，看到 `"ok": true`，而且 `file` 是 `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\feedback.jsonl`。
4. 開機自動啟動：把 `launch_feedback_server.bat` 放在**你電腦本機**（例如 `D:\ASEKH\K20076\2026\公文簽核agent\`），
   `Win+R` → `shell:startup` → 建立它的捷徑（捷徑內容 →「執行」選「最小化」）。
   它會一直等到 .67 連得上才啟動回饋服務；連不上時會自動打開 .67 的資料夾跳出登入視窗，輸入一次密碼即可。
   （公司政策禁止電腦儲存網路密碼，`cmdkey` 會出現「無法從這個登入工作階段儲存認證」。
   要完全免輸入密碼，請 IT 把該資料夾的讀寫權限開給你的網域帳號。）
5. 把新版 `officialdoc.html` 複製到網頁資料夾，打開網頁，右上角顯示「回饋服務：已連線」。
   網頁會先找 `10.10.51.67:8765`，找不到就自動改連你電腦的 `127.0.0.1:8765`，不用改設定。
6. Pensieve：
   * 把 `prompt/comment.txt`、`prompt/summary.txt` 貼進 flow 並**發布**。
   * 確認 **comment 那個 node 讀得到原始的 `input`**（不是只吃 summary node 的輸出），否則「參考回饋」傳不到它手上。
   * 開一個專用知識庫、設定成 replace，掛到 comment 那個 LLM node。

限制：只有你這台電腦上的評分送得出去。之後要讓同事也能評，需要請管 .67 的人在 .67 上跑回饋服務並開 8765 port，
或把網站的 `/feedback` 轉發到回饋服務。

### 驗證

所有指令都在**你的電腦**、`officialdoc_pipeline.py` 所在的資料夾執行（.67 只放檔案，不跑程式）。

0. 先單獨測知識庫上傳：`python officialdoc_pipeline.py --upload-knowledge`，log 出現「知識庫：已上傳」、Pensieve 知識庫裡看得到 `officialdoc_feedback_cases.txt` 就成功。
1. 你電腦執行 `python officialdoc_pipeline.py`，log 要看到「讀入 N 篇公文的使用者回饋」。
2. 網頁上隨便評一篇 3 分並寫原因 → `\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc\feedback.jsonl` 多一行。
3. 再跑一次 pipeline → log 出現「依回饋重產 1」與「知識庫：已上傳」，Pensieve 知識庫裡只有一份 `officialdoc_feedback_cases.txt`。

### 日常怎麼用

* 同一種錯一直出現（看 `feedback_digest.md`）→ 改 `comment.txt` 的規則，貼進 flow 發布，再把 pipeline 的 `PROMPT_VERSION` +1。
  零星的個案修正不用改規則，靠「參考回饋」與知識庫自動帶入即可。
* ⚠️ 這個資料夾就是網頁的 `data` 資料夾，裡面的檔案用瀏覽器也打得開（例如 `/data/officialdoc/feedback.jsonl`）。
  回饋內容被看到問題不大，但 **vector_id 一定要放你電腦**，不要放這裡。
