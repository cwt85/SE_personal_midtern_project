"""PostgreSQL 連線、資料表初始化與 Demo 種子資料。"""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from auth import hash_password


# 讓專案從任何工作目錄啟動時，都能讀到專案根目錄的 .env。
load_dotenv(Path(__file__).resolve().parent / ".env")


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS app_users (
        id BIGSERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL UNIQUE,
        display_name VARCHAR(100) NOT NULL,
        role VARCHAR(20) NOT NULL CHECK (role IN ('author', 'editor')),
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS submission_categories (
        id BIGSERIAL PRIMARY KEY,
        name VARCHAR(80) NOT NULL UNIQUE,
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS submissions (
        id BIGSERIAL PRIMARY KEY,
        author_id BIGINT NOT NULL REFERENCES app_users(id),
        category_id BIGINT NOT NULL REFERENCES submission_categories(id),
        title VARCHAR(200) NOT NULL,
        content TEXT NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'editing'
            CHECK (status IN ('editing', 'in_review', 'approved')),
        revision_note TEXT,
        submitted_at TIMESTAMPTZ,
        reviewed_at TIMESTAMPTZ,
        reviewer_id BIGINT REFERENCES app_users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS submission_reviews (
        id BIGSERIAL PRIMARY KEY,
        submission_id BIGINT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
        editor_id BIGINT NOT NULL REFERENCES app_users(id),
        action VARCHAR(20) NOT NULL CHECK (action IN ('approved', 'returned')),
        note TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_submissions_author_updated ON submissions(author_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_submissions_status_submitted ON submissions(status, submitted_at DESC)",
]


async def initialize_database(pool: AsyncConnectionPool) -> None:
    """建立缺少的表，並放入可直接操作的 Demo 帳號與稿件。"""
    async with pool.connection() as conn:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)

        demo_users = [
            ("author@demo.local", "示範作者", "author", "author1234"),
            ("editor@demo.local", "示範編輯", "editor", "editor1234"),
        ]
        for email, display_name, role, password in demo_users:
            await conn.execute(
                """
                INSERT INTO app_users (email, display_name, role, password_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO NOTHING
                """,
                (email, display_name, role, hash_password(password)),
            )

        category_names = ["校園生活", "科技趨勢", "人物專訪", "心得分享"]
        for name in category_names:
            await conn.execute(
                "INSERT INTO submission_categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (name,),
            )

        # 只在新資料庫第一次初始化時插入範例稿件，之後重啟不重複新增。
        await conn.execute(
            """
            INSERT INTO submissions (author_id, category_id, title, content, status, submitted_at)
            SELECT author.id, category.id, sample.title, sample.content, sample.status,
                   CASE WHEN sample.status = 'in_review' THEN NOW() - INTERVAL '2 hours' ELSE NULL END
            FROM (VALUES
                ('author@demo.local', '校園生活', '讓校園角落成為閱讀空間',
                 '圖書館之外，校園還有許多適合閱讀與交流的角落。本文整理三個可以改善的公共空間，並分享學生實際使用後的想法。', 'editing'),
                ('author@demo.local', '心得分享', '第一次參與校園志工的收穫',
                 '從準備活動到和社區居民互動，志工服務讓我重新認識校園與地方的關係。本文記錄活動過程與幾個值得延續的做法。', 'approved'),
                ('author@demo.local', '科技趨勢', 'AI 工具如何改變學生的學習方式',
                 '生成式 AI 讓整理資料與練習寫作更方便，也帶來查證來源與學術誠信的新課題。本文從課堂情境整理實際觀察。', 'in_review')
            ) AS sample(author_email, category_name, title, content, status)
            JOIN app_users AS author ON author.email = sample.author_email
            JOIN submission_categories AS category ON category.name = sample.category_name
            WHERE NOT EXISTS (SELECT 1 FROM submissions)
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """啟動時連線並準備資料表，關閉時釋放連線。"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("找不到 DATABASE_URL，請先依照 .env.example 建立 .env 並填入 PostgreSQL 連線資訊。")

    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await pool.open()
    await pool.wait()
    app.state.pool = pool
    try:
        await initialize_database(pool)
        yield
    finally:
        await pool.close()


async def get_db(request: Request):
    """將一條資料庫連線交給路由使用，結束後自動歸還連線池。"""
    async with request.app.state.pool.connection() as conn:
        yield conn
