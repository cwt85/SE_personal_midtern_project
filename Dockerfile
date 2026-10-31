FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 用 uv 依照鎖定檔安裝 Python 相依套件。
RUN pip install --no-cache-dir uv

# 複製完整專案與 SQL 初始化檔，再安裝執行所需的相依套件。
COPY . .
RUN uv sync --frozen --no-install-project --no-dev

EXPOSE 8000

# 容器內監聽所有網路介面，讓主機瀏覽器可透過 Compose 映射埠連線。
CMD ["uv", "run", "--no-sync", "fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]
