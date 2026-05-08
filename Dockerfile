# 使用轻量级 Python 基础镜像
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# [新增] 设置环境变量：防止生成 .pyc 文件，确保日志即时输出
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# [新增] 设置默认时区为上海，这对 Cron 调度器非常重要！
ENV TZ=Asia/Shanghai
ARG CHILLPOSTER_VERSION=vdev
ENV CHILLPOSTER_VERSION=$CHILLPOSTER_VERSION
LABEL org.opencontainers.image.version=$CHILLPOSTER_VERSION
RUN echo "$CHILLPOSTER_VERSION" > /app/VERSION

# 安装系统依赖
# [修改] 增加了 tzdata 用于处理时区
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ffmpeg \
    libfreetype6-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    libjpeg-dev \
    zlib1g-dev \
    libimagequant-dev \
    libraqm-dev \
    libtiff-dev \
    libwebp-dev \
    tcl8.6-dev \
    tk8.6-dev \
    python3-tk \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装
COPY requirements.txt .
# 这一步会安装 apscheduler (前提是你更新了 requirements.txt)
RUN pip install --no-cache-dir --default-timeout=300 -r requirements.txt

# 安装 Playwright Chromium 浏览器（影巢签到需要）
RUN playwright install --with-deps chromium

# 复制项目所有文件
COPY . .
# 单独复制默认分类规则（确保不被 .dockerignore 排除）
COPY config/media_organize_category_rules.json config/media_organize_category_rules.json

# =======================================================
# [核心操作] 制作“默认预设”
# =======================================================
# 确保所有文件夹都存在，避免 cp 报错
RUN mkdir -p config fonts templates layouts backups defaults/config defaults/templates defaults/layouts defaults/fonts \
    && cp -r config/* defaults/config/ 2>/dev/null || : \
    && cp -r templates/* defaults/templates/ 2>/dev/null || : \
    && cp -r layouts/* defaults/layouts/ 2>/dev/null || : \
    && cp -r fonts/* defaults/fonts/ 2>/dev/null || :

# 暴露端口
EXPOSE 5256

# 设置数据卷
VOLUME ["/app/config", "/app/templates", "/app/layouts", "/app/fonts", "/app/backups"]

# 启动命令
CMD ["python", "main.py"]