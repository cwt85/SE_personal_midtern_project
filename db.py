"""PostgreSQL 連線與 SQL 初始化檔載入。"""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from auth import hash_password


BASE_DIR = Path(__file__).resolve().parent

# 讓專案從任何工作目錄啟動時，都能讀到專案根目錄的 .env。
load_dotenv(BASE_DIR / ".env")


async def initialize_database(pool: AsyncConnectionPool) -> None:
    """讀取 project.sql，填入 Demo 帳號的密碼雜湊後逐段執行。"""
    sql_file = BASE_DIR / "project.sql"
    if not sql_file.exists():
        raise FileNotFoundError(f"找不到資料庫初始化檔：{sql_file}")

    sql_script = sql_file.read_text(encoding="utf-8-sig")
    # 密碼雜湊由 Python 產生；SQL 檔只保留替換標記，不保存明文密碼。
    sql_script = sql_script.replace("{{AUTHOR_PASSWORD_HASH}}", hash_password("author1234"))
    sql_script = sql_script.replace("{{EDITOR_PASSWORD_HASH}}", hash_password("editor1234"))

    async with pool.connection() as conn:
        # 初始化檔以分號結尾區分指令；移除純註解行後逐條交給 PostgreSQL。
        for raw_statement in sql_script.split(";"):
            statement = "\n".join(
                line for line in raw_statement.splitlines()
                if line.strip() and not line.lstrip().startswith("--")
            ).strip()
            if statement:
                await conn.execute(statement)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """啟動時連線並準備資料表，關閉時釋放連線。"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("找不到 DATABASE_URL，請先依照 .env.example 建立 .env 並填入 PostgreSQL 連線資訊。")

    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={
            "row_factory": dict_row,
            # 每條連線的工作階段時區固定為 UTC+8（台灣時間）。
            # TIMESTAMPTZ 在資料庫內一律以 UTC 儲存，讀出時會依此時區換算，
            # 因此不論 PostgreSQL 主機（例如 Docker 容器預設 UTC）設定為何，畫面都顯示台灣時間。
            "options": "-c timezone=Asia/Taipei",
        },
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
