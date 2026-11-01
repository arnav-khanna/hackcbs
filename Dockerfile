
FROM python:3.11

WORKDIR /edge-resq-app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir "mcp[cli]>=1.2,<2"

COPY ./edge-resq-app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
