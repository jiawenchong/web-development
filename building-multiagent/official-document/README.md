# 建廠影響公文掃描

從公文中找出「外部單位的事情」會讓建廠暫停、延後或需要配合的公文
（例如消防檢查當天全面停工、勞檢暫停施工、台電停電、道路封閉），
整理成固定格式的 JSON，之後再跟其他 agent 整合。

在**你自己的電腦**上跑，不經過 .170。

## 資料夾

全部在 `D:\ASEKH\K20076\2027\Agent\Building MultiAGENT\official document`
（`construction_impact_scan.py` 的 `BASE_DIR`），子資料夾第一次執行時自動建立：

```
official document\
├─ construction_impact_scan.py      主程式
├─ prompt\construction_impact.txt   給 LLM 的 prompt
├─ api_key.txt                      （選用）API key，單行。不要 commit
├─ input\
│  ├─ pdf\        要判斷的公文（PDF / PNG / JPG，可以有子資料夾）
│  ├─ history\    以前的公文，整理成歷史案例給 RAG
│  └─ report\     （選用）環安衛報表 .xls，有放就拿主旨跟 LLM 讀到的對照
├─ output\
│  ├─ construction_impact.json    還沒過去的影響，停工排最前面
│  ├─ history_cases.json          已經過去的案例 + input\history 的公文
│  └─ not_related.json            粗篩排除 / LLM 判定無關（檢查有沒有漏）
└─ cache\         判斷快取、log
```

想直接讀 U: 的資料夾，在設定區 `PDF_FOLDERS` 多加一行路徑就好，不用複製檔案。

## 流程

1. **掃檔案**：從檔名取日期/字號/發文單位（`1150901_11512300_高局_..._.pdf` → 2026-09-01、11512300、高局）。
2. **日期篩選**：`input\pdf` 只看檔名日期在最近 `DAYS`（預設 90）天內的。`input\history` 不篩日期。
3. **關鍵字粗篩**：檔名、報表主旨、PDF 文字層出現施工/消防/停電/廠號等關鍵字就送。
   **讀不到文字的掃描檔一律送**，寧可多送不要漏。
4. **LLM 判斷**：每頁轉成 PNG（長邊 1600px，最多 20 頁）→ 上傳 `/files` 拿 `file_id`
   → 跟 prompt 一起送 `/chat/completions`。`file_id` 每次都重新上傳，不重複使用。
5. **分流輸出**：
   * 結束日期（沒有就看開始日期）還沒過 → `construction_impact.json`
   * 已經過了，或來自 `input\history` → `history_cases.json`
   * 這次在目前清單裡的，日期過了之後下次執行會自動移到歷史
   * LLM 判定與建廠無關、或粗篩排除 → `not_related.json`

## 每一筆的欄位

| 欄位 | 說明 |
|---|---|
| 來文字號 / 主旨 / 發文機關 / 發文日期 | LLM 從公文讀出來的 |
| 是否建廠相關 | true / false（判斷失敗是 null） |
| 影響程度 | 全面停工 / 部分停工 / 需配合作業 / 僅需文件回覆 / 無影響 / 待確認 |
| 原因類別 | 消防檢查 / 勞動檢查 / 建管查驗 / 環保稽查 / 停電停水 / 道路封閉 / 會勘 / 停工令 / 審查未過 / 其他 |
| 影響廠棟 / 開始日期 / 結束日期 / 時段 | 日期一律 YYYY-MM-DD，民國年程式會再轉一次 |
| 需要配合事項 | 一句話 |
| 原文依據 | LLM 逐字抄錄的原文 |
| 信心度 | 高 / 中 / 低 |
| 依據檢查 | 符合原文 / 原文找不到 / 掃描檔，無法自動比對 / 沒有原文依據 |
| 需人工確認 | true 的要人看一下 |
| 檢查備註 | 為什麼要人工確認 |

**防止亂編**：PDF 有文字層時，程式會檢查「原文依據」是不是真的出現在原文裡，
找不到就把信心度降成「低」並標「需人工確認」。有影響但沒有日期、LLM 讀到的主旨跟報表不同、
判斷失敗的，也都會標「需人工確認」。判斷失敗的不會進快取，下次執行會重試。

## 第一次設定

1. `pip install pymupdf`（有放報表才需要：`pip install pandas xlrd lxml`）
2. 打開 `construction_impact_scan.py` 設定區，填 `AGENT_ID`；API key 三選一：
   填 `API_KEY`、設環境變數 `ASE_LLM_API_KEY`、或存成 `api_key.txt`。
   key 和 agent id 要成對，混用會回 401。
3. 把公文 PDF 放進 `input\pdf`，以前的放進 `input\history`。

## 怎麼跑

```bash
python construction_impact_scan.py --dry-run          # 不呼叫 LLM，看掃到哪些、粗篩留下哪些、排除哪些
python construction_impact_scan.py --probe            # 只送粗篩後第一篇，印出原始回應
python construction_impact_scan.py --probe-file "input\pdf\xxx.pdf"   # 指定一篇測試
python construction_impact_scan.py                    # 正式跑
```

其他選項：

* `--days 30` / `--since 2026-09-01`：改日期範圍
* `--all`：不做關鍵字粗篩，日期範圍內全部送
* `--no-cache`：全部重新判斷
* `--skip-history`：這次不處理 `input\history`

改了 `prompt\construction_impact.txt` 之後，把程式裡的 `PROMPT_VERSION` +1，不然會直接用舊快取。

## 注意

* 圖片只送 PNG，不用 base64 塞進訊息（平台讀不到）。
* 超過 20 頁的公文只送前 20 頁，`檢查備註` 會寫出來；常遇到 504 就調低 `MAX_PAGES`。
* 檔名第一段不是日期的檔案沒辦法做日期篩選，一律保留，log 會列出來。
