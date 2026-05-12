cd ton-repo
cat > Dockerfile << 'EOF'
FROM python:3.11-slim

RUN apt-get update && apt-get install -y libxslt1.1 libxml2 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
EOF

git add Dockerfile
git commit -m "fix: Dockerfile with libxslt"
git push
