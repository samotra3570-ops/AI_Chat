FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY gateway ./gateway
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ACM_STATE_DIR=/data/acm ACM_BIND_HOST=0.0.0.0 ACM_RUN_UID=10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=3)"
ENTRYPOINT ["python", "-m", "gateway.deploy"]
CMD ["serve"]
