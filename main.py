"""投稿審查 Demo：作者管理稿件，編輯審查送出的稿件。"""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import SESSION_TTL_SECONDS, create_session_token, read_session_token, verify_password
from db import get_db, lifespan


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="投稿審查系統", description="作者投稿、編輯審查與稿件狀態追蹤 Demo", lifespan=lifespan)
SESSION_COOKIES = {"author": "submission_author_session", "editor": "submission_editor_session"}
WORKSPACES = {"author": "/author/submissions", "editor": "/editor/reviews"}


def read_role_session(request: Request, role: str) -> dict | None:
    """Cookie 名稱只用來選擇身分；實際角色仍須通過簽章驗證。"""
    user = read_session_token(request.cookies.get(SESSION_COOKIES[role]))
    return user if user and user["role"] == role else None


@app.middleware("http")
async def load_current_user(request: Request, call_next):
    """依工作區選擇 Cookie，避免同一瀏覽器的作者與編輯互相覆蓋。"""
    role = request.url.path.split("/")[1]
    request.state.user = read_role_session(request, role) if role in SESSION_COOKIES else None
    return await call_next(request)


def require_role(request: Request, expected_role: str) -> dict:
    """要求登入與指定角色；不符合時回到登入頁或顯示禁止存取。"""
    user = request.state.user
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if user["role"] != expected_role:
        raise HTTPException(status_code=403, detail="目前帳號沒有使用此功能的權限")
    return user


def render(request: Request, page: str, context: dict | None = None, status_code: int = 200):
    """統一將登入者資訊放進畫面，避免每個路由重複處理。"""
    page_context = {"user": request.state.user, **(context or {})}
    if page == "login.html":
        # 登入頁可返回既有工作區，也能在不登出的情況下登入另一角色。
        page_context["active_sessions"] = {
            role: user for role in SESSION_COOKIES if (user := read_role_session(request, role))
        }
    response = templates.TemplateResponse(
        name=page,
        request=request,
        context=page_context,
        status_code=status_code,
    )
    # 使用者切換角色或送審後，返回上一頁時不應看到過期的稿件狀態。
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def status_label(status: str) -> str:
    """把資料庫狀態轉成需求圖上的中文名稱。"""
    return {"editing": "編輯中", "in_review": "審查中", "approved": "已通過"}.get(status, status)


@app.get("/", include_in_schema=False)
async def home(request: Request):
    """只有一個角色登入時直接進入；兩者皆登入時提供工作區選擇。"""
    roles = [role for role in SESSION_COOKIES if read_role_session(request, role)]
    destination = WORKSPACES[roles[0]] if len(roles) == 1 else "/login"
    return RedirectResponse(url=destination, status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """保留登入入口，允許另一個分頁登入不同角色。"""
    return render(request, "login.html", {"error": None})


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db),
):
    """驗證帳密，並把簽章後的登入狀態放進瀏覽器 Cookie。"""
    cursor = await db.execute(
        "SELECT id, display_name, role, password_hash FROM app_users WHERE email = %s",
        (email.strip().lower(),),
    )
    user = await cursor.fetchone()
    if user is None or not verify_password(password, user["password_hash"]):
        return render(request, "login.html", {"error": "帳號或密碼不正確。"}, status_code=401)

    # 登入後直接進入該角色的工作區，不讓另一角色的 Cookie 決定目的地。
    response = RedirectResponse(url=WORKSPACES[user["role"]], status_code=303)
    response.set_cookie(
        SESSION_COOKIES[user["role"]],
        create_session_token(user["id"], user["role"], user["display_name"]),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # 本機 HTTP Demo 使用；正式 HTTPS 站台應改成 True。
    )
    # 舊版共用 Cookie 不再參與驗證，登入成功時順便移除。
    response.delete_cookie("submission_session")
    return response


@app.post("/logout")
async def logout(role: str = Form(...)):
    """僅登出表單所屬角色，保留另一個工作區的登入狀態。"""
    if role not in SESSION_COOKIES:
        raise HTTPException(status_code=400, detail="無效的登出角色")
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIES[role])
    return response


@app.get("/author/submissions", response_class=HTMLResponse)
async def author_submissions(
    request: Request,
    status: str = "all",
    q: str = "",
    notice: str = "",
    db=Depends(get_db),
):
    """作者只能看到自己的稿件，可依狀態與標題篩選。"""
    author = require_role(request, "author")
    valid_statuses = {"all", "editing", "in_review", "approved"}
    if status not in valid_statuses:
        status = "all"
    query = """
        SELECT s.id, s.title, s.status, s.updated_at, s.revision_note,
               c.name AS category_name
        FROM submissions AS s
        JOIN submission_categories AS c ON c.id = s.category_id
        WHERE s.author_id = %s
    """
    params: list = [author["id"]]
    if status != "all":
        query += " AND s.status = %s"
        params.append(status)
    if q.strip():
        query += " AND s.title ILIKE %s"
        params.append(f"%{q.strip()}%")
    query += " ORDER BY s.updated_at DESC, s.id DESC"
    cursor = await db.execute(query, tuple(params))
    submissions = await cursor.fetchall()

    counts_cursor = await db.execute(
        "SELECT status, COUNT(*) AS total FROM submissions WHERE author_id = %s GROUP BY status",
        (author["id"],),
    )
    counts = {row["status"]: row["total"] for row in await counts_cursor.fetchall()}
    return render(
        request,
        "author_submissions.html",
        {"submissions": submissions, "counts": counts, "status": status, "q": q, "notice": notice, "status_label": status_label},
    )


async def get_categories(db) -> list[dict]:
    """只提供啟用中的投稿分類。"""
    cursor = await db.execute(
        "SELECT id, name FROM submission_categories WHERE is_active = TRUE ORDER BY name"
    )
    return await cursor.fetchall()


@app.get("/author/submissions/new", response_class=HTMLResponse)
async def new_submission_page(request: Request, db=Depends(get_db)):
    """顯示新增稿件表單。"""
    require_role(request, "author")
    return render(request, "submission_form.html", {"submission": None, "categories": await get_categories(db), "error": None})


@app.post("/author/submissions/new")
async def create_submission(
    request: Request,
    title: str = Form(..., max_length=200),
    category_id: int = Form(...),
    content: str = Form(...),
    action: str = Form("save"),
    db=Depends(get_db),
):
    """新增稿件；作者可以先存成編輯中，也可以直接送審。"""
    author = require_role(request, "author")
    title = title.strip()
    content = content.strip()
    categories = await get_categories(db)
    if not title or not content or category_id not in {item["id"] for item in categories}:
        return render(
            request,
            "submission_form.html",
            {"submission": {"title": title, "content": content, "category_id": category_id}, "categories": categories, "error": "請填寫標題、分類與文章內容。"},
            status_code=400,
        )
    status = "in_review" if action == "submit" else "editing"
    cursor = await db.execute(
        """
        INSERT INTO submissions (author_id, category_id, title, content, status, submitted_at)
        VALUES (%s, %s, %s, %s, %s, CASE WHEN %s = 'in_review' THEN NOW() ELSE NULL END)
        RETURNING id
        """,
        (author["id"], category_id, title, content, status, status),
    )
    submission_id = (await cursor.fetchone())["id"]
    notice = "稿件已送出，等待編輯審查。" if status == "in_review" else "草稿已儲存。"
    return RedirectResponse(url=f"/author/submissions/{submission_id}/edit?notice={notice}", status_code=303)


async def load_owned_submission(db, submission_id: int, author_id: int) -> dict:
    """載入作者自己的稿件，避免透過網址讀寫其他作者的內容。"""
    cursor = await db.execute(
        """
        SELECT s.*, c.name AS category_name
        FROM submissions AS s
        JOIN submission_categories AS c ON c.id = s.category_id
        WHERE s.id = %s AND s.author_id = %s
        """,
        (submission_id, author_id),
    )
    submission = await cursor.fetchone()
    if submission is None:
        raise HTTPException(status_code=404, detail="找不到這篇稿件")
    return submission


@app.get("/author/submissions/{submission_id}/edit", response_class=HTMLResponse)
async def edit_submission_page(request: Request, submission_id: int, notice: str = "", db=Depends(get_db)):
    """顯示草稿或退回稿件；審查中與通過的稿件不可編輯。"""
    author = require_role(request, "author")
    submission = await load_owned_submission(db, submission_id, author["id"])
    if submission["status"] != "editing":
        message = f"稿件狀態已更新為「{status_label(submission['status'])}」，目前不能編輯。清單已重新整理。"
        return RedirectResponse(url="/author/submissions?" + urlencode({"notice": message}), status_code=303)
    return render(
        request,
        "submission_form.html",
        {"submission": submission, "categories": await get_categories(db), "error": None, "notice": notice},
    )


@app.post("/author/submissions/{submission_id}/edit")
async def update_submission(
    request: Request,
    submission_id: int,
    title: str = Form(..., max_length=200),
    category_id: int = Form(...),
    content: str = Form(...),
    action: str = Form("save"),
    db=Depends(get_db),
):
    """更新編輯中的稿件，送審時清除舊退回意見並更新狀態。"""
    author = require_role(request, "author")
    submission = await load_owned_submission(db, submission_id, author["id"])
    if submission["status"] != "editing":
        message = f"稿件狀態已更新為「{status_label(submission['status'])}」，本次修改沒有儲存。"
        return RedirectResponse(url="/author/submissions?" + urlencode({"notice": message}), status_code=303)
    title = title.strip()
    content = content.strip()
    categories = await get_categories(db)
    if not title or not content or category_id not in {item["id"] for item in categories}:
        submission.update({"title": title, "content": content, "category_id": category_id})
        return render(
            request,
            "submission_form.html",
            {"submission": submission, "categories": categories, "error": "請填寫標題、分類與文章內容。"},
            status_code=400,
        )
    status = "in_review" if action == "submit" else "editing"
    await db.execute(
        """
        UPDATE submissions
        SET title = %s, category_id = %s, content = %s, status = %s,
            submitted_at = CASE WHEN %s = 'in_review' THEN NOW() ELSE submitted_at END,
            revision_note = CASE WHEN %s = 'in_review' THEN NULL ELSE revision_note END,
            updated_at = NOW()
        WHERE id = %s AND author_id = %s
        """,
        (title, category_id, content, status, status, status, submission_id, author["id"]),
    )
    notice = "稿件已送出，等待編輯審查。" if status == "in_review" else "稿件已儲存。"
    return RedirectResponse(url=f"/author/submissions/{submission_id}/edit?notice={notice}", status_code=303)


@app.post("/author/submissions/{submission_id}/delete")
async def delete_submission(request: Request, submission_id: int, db=Depends(get_db)):
    """作者只能刪除自己尚在編輯中的稿件。"""
    author = require_role(request, "author")
    submission = await load_owned_submission(db, submission_id, author["id"])
    if submission["status"] != "editing":
        message = f"稿件狀態已更新為「{status_label(submission['status'])}」，不能刪除。"
        return RedirectResponse(url="/author/submissions?" + urlencode({"notice": message}), status_code=303)
    await db.execute("DELETE FROM submissions WHERE id = %s AND author_id = %s", (submission_id, author["id"]))
    return RedirectResponse(url="/author/submissions", status_code=303)


@app.get("/editor/reviews", response_class=HTMLResponse)
async def editor_reviews(request: Request, view: str = "pending", db=Depends(get_db)):
    """編輯查看待審稿件，亦可切換檢視已完成的審查紀錄。"""
    require_role(request, "editor")
    if view not in {"pending", "completed"}:
        view = "pending"
    statuses = ("in_review",) if view == "pending" else ("approved", "editing")
    cursor = await db.execute(
        """
        SELECT s.id, s.title, s.status, s.submitted_at, u.display_name AS author_name,
               c.name AS category_name
        FROM submissions AS s
        JOIN app_users AS u ON u.id = s.author_id
        JOIN submission_categories AS c ON c.id = s.category_id
        WHERE s.status = ANY(%s)
          AND (%s = 'pending' OR s.status = 'approved' OR EXISTS (
              SELECT 1 FROM submission_reviews AS prior_review
              WHERE prior_review.submission_id = s.id AND prior_review.action = 'returned'
          ))
        ORDER BY s.submitted_at DESC NULLS LAST, s.id DESC
        """,
        (list(statuses), view),
    )
    submissions = await cursor.fetchall()
    count_cursor = await db.execute(
        "SELECT status, COUNT(*) AS total FROM submissions GROUP BY status"
    )
    counts = {row["status"]: row["total"] for row in await count_cursor.fetchall()}
    return render(
        request,
        "editor_reviews.html",
        {"submissions": submissions, "counts": counts, "view": view, "status_label": status_label},
    )


@app.get("/editor/reviews/{submission_id}", response_class=HTMLResponse)
async def review_detail(request: Request, submission_id: int, notice: str = "", db=Depends(get_db)):
    """顯示稿件全文、作者資訊與過往審查紀錄。"""
    require_role(request, "editor")
    cursor = await db.execute(
        """
        SELECT s.*, u.display_name AS author_name, u.email AS author_email,
               c.name AS category_name
        FROM submissions AS s
        JOIN app_users AS u ON u.id = s.author_id
        JOIN submission_categories AS c ON c.id = s.category_id
        WHERE s.id = %s
        """,
        (submission_id,),
    )
    submission = await cursor.fetchone()
    if submission is None:
        raise HTTPException(status_code=404, detail="找不到這篇稿件")
    history_cursor = await db.execute(
        """
        SELECT r.action, r.note, r.created_at, u.display_name AS editor_name
        FROM submission_reviews AS r
        JOIN app_users AS u ON u.id = r.editor_id
        WHERE r.submission_id = %s
        ORDER BY r.created_at DESC, r.id DESC
        """,
        (submission_id,),
    )
    history = await history_cursor.fetchall()
    return render(
        request,
        "editor_detail.html",
        {"submission": submission, "history": history, "notice": notice, "status_label": status_label},
    )


@app.post("/editor/reviews/{submission_id}")
async def decide_submission(
    request: Request,
    submission_id: int,
    action: str = Form(...),
    note: str = Form(""),
    db=Depends(get_db),
):
    """編輯通過稿件，或填寫退回原因並交還作者修改。"""
    editor = require_role(request, "editor")
    if action not in {"approve", "return"}:
        raise HTTPException(status_code=400, detail="無效的審查動作")
    note = note.strip()
    if action == "return" and not note:
        raise HTTPException(status_code=400, detail="退回稿件時請填寫修改建議")

    cursor = await db.execute(
        "SELECT id, status FROM submissions WHERE id = %s",
        (submission_id,),
    )
    submission = await cursor.fetchone()
    if submission is None:
        raise HTTPException(status_code=404, detail="找不到這篇稿件")
    if submission["status"] != "in_review":
        raise HTTPException(status_code=409, detail="這篇稿件已經完成審查或不在待審狀態")

    new_status = "approved" if action == "approve" else "editing"
    review_action = "approved" if action == "approve" else "returned"
    async with db.transaction():
        update_cursor = await db.execute(
            """
            UPDATE submissions
            SET status = %s, revision_note = %s, reviewer_id = %s,
                reviewed_at = NOW(), updated_at = NOW()
            WHERE id = %s AND status = 'in_review'
            RETURNING id
            """,
            (new_status, note if action == "return" else None, editor["id"], submission_id),
        )
        if await update_cursor.fetchone() is None:
            # 條件式更新可避免兩位編輯同時處理時，重複寫入審查結果。
            raise HTTPException(status_code=409, detail="這篇稿件已經被其他編輯處理")
        await db.execute(
            """
            INSERT INTO submission_reviews (submission_id, editor_id, action, note)
            VALUES (%s, %s, %s, %s)
            """,
            (submission_id, editor["id"], review_action, note),
        )
    notice = "稿件已通過。" if action == "approve" else "稿件已退回作者修改。"
    return RedirectResponse(url=f"/editor/reviews/{submission_id}?notice={notice}", status_code=303)


@app.get("/health", include_in_schema=False)
async def health():
    """提供簡單的 HTTP 活性檢查，方便 Demo 時確認服務已啟動。"""
    return {"status": "ok"}

