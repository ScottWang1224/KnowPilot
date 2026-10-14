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

## 認證與權限

- `POST /api/auth/login` 回傳 JWT，前端存在 localStorage，以 `Authorization: Bearer` 呼叫 API。
- JWT 只含 `sub` 與 `exp`；部門、角色、啟用狀態每次請求都從資料庫讀取。
- 文件權限只由部門決定（`repositories/content.py` 的 `document_scope()`），角色只決定能否使用 `/api/admin/*`。
- 頁面路由不做登入判斷，資料一律經過 API 的 `get_current_user`／`require_admin`。

## 正式環境（MySQL 8.0.16+）

設定 `APP_ENV=production`、`DB_ENGINE=mysql`、`DB_*`、`DB_SSL_CA`、`JWT_SECRET` 與 `ALLOWED_HOSTS`，
再執行 `python -m scripts.migrate`。應用程式啟動時不會自動執行 migration。

## Schema 變更

新增 `migrations/mysql/NNN_xxx.sql` 與對應的 `migrations/sqlite/NNN_xxx.sql`，不要修改已套用的檔案。

## 測試

```bash
.venv/Scripts/python -m pytest -q
RUN_BROWSER_TESTS=1 .venv/Scripts/python -m pytest -q tests/test_browser.py   # 截圖存於 .test-artifacts/
```

MySQL 整合測試需設定 `KNOWPILOT_TEST_DB_*` 指向空的 `knowpilot_test_*` 資料庫。
