FROM docker:29-cli

RUN apk add --no-cache python3 py3-pip py3-virtualenv docker-cli-compose

WORKDIR /app

COPY requirements.txt .
RUN virtualenv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"

COPY main.py index.html ./
COPY static ./static

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
