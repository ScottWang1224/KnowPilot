# KnowPilot

製造業企業內部 AI 知識助理（FastAPI + Jinja2）。

## 本機開發（SQLite，需要 Python 3.11 以上）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # 本機開發用；正式環境只裝 requirements.txt
cp .env.example .env                      # 填入 JWT_SECRET；預設 DB_ENGINE=sqlite，資料存在 .data/
.venv/Scripts/python -m scripts.migrate
.venv/Scripts/python -m scripts.seed        # 建立虛構部門、公告與文件
.venv/Scripts/python -m scripts.seed_admin  # 建立管理員帳號（唯一的 admin 來源）
.venv/Scripts/python -m uvicorn app:app --reload
```

一般帳號從 `/register` 註冊，註冊時可自選部門（Demo 設計，可在個人資料頁切換）。

## 展示資料（`data/`）

- `data/documents/{部門}/*.md`、`data/announcements/*.md`：Markdown + YAML front matter，由 `scripts.seed` 匯入；只新增、不覆蓋（以 `document_code`／`seed_key` 判斷）。
- `data/eval/questions.yaml`：RAG 評測題目。
- 所有公司、人員、客戶、設備與數值皆為虛構展示資料。
- 已存在的文件不會被 seed 更新；本機要套用改寫後的內容，需刪除 `.data/knowpilot.db` 後重新 migrate 與 seed。

## 認證與權限

- `POST /api/auth/login` 回傳 JWT，前端存在 localStorage，以 `Authorization: Bearer` 呼叫 API。
- JWT 只含 `sub` 與 `exp`；部門、角色、啟用狀態每次請求都從資料庫讀取。
- 文件權限只由部門決定（`repositories/content.py` 的 `document_scope()`），角色只決定能否使用 `/api/admin/*`。
- 頁面路由不做登入判斷，資料一律經過 API 的 `get_current_user`／`require_admin`。

## 文件提交與審核

- 任何使用者都可以在文件中心「新增文件」，或在自己部門的文件頁「提出修改」；內容為 Markdown（網頁編輯或上傳 UTF-8 `.md`，200 KB 以內，至少一個以 `##` 開頭的章節標題）。
- 提交存在 `document_submissions`，**審核通過前不會進入 `documents`**，因此文件中心、`document_scope()` 與 AI 助理的檢索都看不到草稿。
- 管理員在「後台 → 文件審核」發布或退回（退回須填原因）；發布時核發代碼（`{部門}-{類別}-{流水號}`）或依小改／大改進版，舊版存進 `document_revisions`，並立即重建 RAG 索引，不需重新部署。管理員也可在後台文件頁下架文件。
- 提交內容視為不可信輸入：疑似寫給 AI 的指令、外部連結、HTML 標籤與同部門重複標題會在審核頁提醒（不阻擋）。術語表 `QA-TRN-001` 會進入所有部門的路由提示，不開放經由提交修改。
- 每人同時最多 5 筆待審、每日最多送出 10 次；切換部門時，待審的提交會自動撤回。
- RAG 索引在單一 process 的記憶體內，發布後只有該 process 立即生效，所以只能跑單一 uvicorn worker。

## 正式環境（MySQL 8.0.16+）

設定 `APP_ENV=production`、`DB_ENGINE=mysql`、`DB_*`、`DB_SSL_CA`、`JWT_SECRET` 與 `ALLOWED_HOSTS`，
再執行 `python -m scripts.migrate`。應用程式啟動時不會自動執行 migration。

## Schema 變更

新增 `migrations/mysql/NNN_xxx.sql` 與對應的 `migrations/sqlite/NNN_xxx.sql`，不要修改已套用的檔案。

## 測試

```bash
.venv/Scripts/python -m pytest -q
RUN_BROWSER_TESTS=1 .venv/Scripts/python -m pytest -q   # 含瀏覽器測試，截圖存於 .test-artifacts/
```

MySQL 整合測試需設定 `KNOWPILOT_TEST_DB_*` 指向空的 `knowpilot_test_*` 資料庫。
