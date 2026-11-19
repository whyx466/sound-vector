FROM python:3.11-slim

# 定时触发：容器内 cron（微信云托管官方实践）
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron curl \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

WORKDIR /app

COPY requirements.txt .
# 腾讯内网源优先（构建机在腾讯机房），失败自动回退清华/阿里/官方源
RUN pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple \
    || pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ \
    || pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt

COPY . .

RUN mkdir -p collector/out/charts \
    && chmod +x start.sh task.sh

# 安装定时任务（task.txt 为 crontab 格式）
RUN crontab task.txt

EXPOSE 8080

ENTRYPOINT ["sh", "start.sh"]
