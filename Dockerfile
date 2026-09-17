FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pytest.ini ./pytest.ini

EXPOSE 8800

# 配置文件由部署侧挂载：-v /opt/ai-alarm/config.yaml:/app/config.yaml
# 时区：日报按北京时间统计，必须设置 TZ
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
