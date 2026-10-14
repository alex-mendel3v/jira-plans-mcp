FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 appuser

COPY jira_client.py server.py ./

USER appuser

EXPOSE 8080

CMD ["python", "server.py"]
