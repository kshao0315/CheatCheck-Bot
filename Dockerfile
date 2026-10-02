FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py account_pool.py runtime_config.py join_policy.py group_imports.py join_control.py group_whitelist.py .
CMD ["python", "bot.py"]
