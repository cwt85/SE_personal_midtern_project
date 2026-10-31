# 投稿審查系統 Demo

依照期中需求製作的雙角色投稿流程。作者可以建立、編輯、刪除草稿並送出審查；編輯可以閱讀待審稿件、通過稿件，或留下修改建議退回作者。

## 快速啟動

### 1. 準備 PostgreSQL 連線

確認本機 PostgreSQL 服務正在執行，複製環境設定範本：

```powershell
Copy-Item .env.example .env
```

打開 `.env`，將 `DATABASE_URL` 的 `YOUR_PASSWORD` 換成自己的 PostgreSQL 密碼。範例連到本機 `postgres` 資料庫；啟動時會在該資料庫建立投稿系統專用資料表，不會刪除既有資料表。請勿把 `.env` 提交到 Git。

### 2. 安裝套件並啟動

```powershell
uv sync
uv run fastapi dev main.py --host 127.0.0.1 --port 8081
```

在瀏覽器開啟 `http://127.0.0.1:8081`。若 8081 已被占用，可換成其他未使用的埠號。`/health` 可確認 HTTP 服務是否已啟動，`/docs` 可查看 API。

第一次啟動會讀取 `project.sql`，自動建立資料表、Demo 帳號、投稿分類與三篇範例稿件。之後重啟不會重複建立範例資料。所有建表與初始化 SQL 都集中在這個檔案；`db.py` 負責讀取、替換密碼雜湊標記並執行。

## 用 Docker 執行

先啟動 Docker Desktop，開啟 PowerShell 並切換到專案資料夾，再執行：

```powershell
docker compose up --build
```

第一次會下載 Python 與 PostgreSQL 映像並建置網站容器，完成後開啟 `http://127.0.0.1:8081`。Docker Compose 會一起啟動 PostgreSQL，不需要連接 Windows 上的 PostgreSQL 服務；網站容器透過 Docker 內部的 `db` 名稱連資料庫。資料庫使用 Docker named volume 保存，重新啟動容器後資料仍會保留。

停止服務可在執行中的視窗按 `Ctrl+C`，再執行：

```powershell
docker compose down
```

`docker compose down` 會停止並移除容器，但保留資料庫 volume。若已在背景啟動，可用 `docker compose logs -f app` 查看網站啟動訊息。

## Demo 帳號

| 角色 | 電子郵件 | 密碼 |
| --- | --- | --- |
| 作者 | `author@demo.local` | `author1234` |
| 編輯 | `editor@demo.local` | `editor1234` |

建議 Demo 順序：作者登入並新增稿件，先存草稿後送審；登出後以編輯登入，開啟待審稿件並退回，填寫修改建議；再以作者登入編輯並重新送審；最後以編輯登入通過稿件。兩個角色各自有權限限制。

## 資料庫設計

```text
app_users (作者與編輯帳號)
    1 ─── 多 submissions (稿件) 多 ─── 1 submission_categories (分類)
                    1
                    └── 多 submission_reviews (審查歷程)
                              多 ─── 1 app_users (執行審查的編輯)
```

- `app_users`：登入帳號、密碼雜湊與角色。
- `submission_categories`：稿件分類，稿件用外鍵參照分類。
- `submissions`：稿件內容、作者、狀態、送審時間與最近一次退回意見。
- `submission_reviews`：每一次通過或退回的歷程與編輯意見。稿件退回後狀態回到「編輯中」，歷程仍保留。

資料庫狀態使用 `editing`、`in_review`、`approved`，畫面分別顯示為「編輯中」、「審查中」、「已通過」。這讓退回修改自然回到可編輯狀態，同時由審查歷程保留退回紀錄。

## 路由概覽

- `GET /login`：登入頁。
- `GET /author/submissions`：作者自己的稿件清單與狀態篩選。
- `GET /author/submissions/new`：新增稿件。
- `GET /editor/reviews`：編輯的待審清單與已處理清單。
- `GET /editor/reviews/{id}`：稿件全文、審查操作與審查歷程。
- `GET /docs`：FastAPI 互動式 API 文件。

## 資料安全與限制

- 密碼使用 PBKDF2 雜湊後保存；Cookie 有簽章與期限。
- 作者只能操作自己的編輯中稿件；編輯只能處理審查中的稿件。
- 這是課堂 Demo，登入帳號由程式初始化，不包含註冊、管理員後台、CSRF 防護或正式部署用的 HTTPS 設定。
