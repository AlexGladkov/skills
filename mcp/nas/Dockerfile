FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mcp/nas/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cli/nasctl/nasctl.py ./nasctl.py
COPY mcp/nas/src/server.py ./server.py
USER 10001:10001
EXPOSE 8000
CMD ["python", "server.py"]
