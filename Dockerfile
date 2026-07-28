# Render 외의 호스팅(Railway, Fly.io, Cloud Run, 자체 서버 등)을 쓸 때 사용.
# Render는 render.yaml만으로 충분해서 이 파일이 없어도 된다.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sources.py server.py ./

EXPOSE 8000
CMD ["python", "server.py"]
