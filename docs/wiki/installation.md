# 安装部署

ChillPoster 支持 Docker/NAS 部署，也支持本地源码运行。生产使用推荐 Docker，开发调试推荐源码运行。

## 环境要求

- 一台可长期运行的 Linux/NAS/macOS 主机
- 可访问 Emby Server 的网络环境
- Docker 与 Docker Compose，生产部署推荐
- Python 依赖环境，本地开发时需要
- Node.js 与 npm，仅构建前端时需要

## Docker 快速部署

```bash
docker run -d \
  --name chillposter \
  -p 5256:5256 \
  -v /volume1/docker/chillposter/config:/app/config \
  -v /volume1/docker/chillposter/fonts:/app/fonts \
  -v /volume1/docker/chillposter/templates:/app/templates \
  -v /volume1/docker/chillposter/layouts:/app/layouts \
  chillne/chillposter:latest
```

启动后访问：

```text
http://NAS_IP:5256
```

## Docker Compose 示例

```yaml
services:
  chillposter:
    image: chillne/chillposter:latest
    container_name: chillposter
    restart: unless-stopped
    ports:
      - "5256:5256"
    volumes:
      - ./config:/app/config
      - ./fonts:/app/fonts
      - ./templates:/app/templates
      - ./layouts:/app/layouts
      - ./backups:/app/backups
```

如果启用了 302 网关，请在 Compose 中额外映射对应网关端口。例如配置网关端口为 `8011`，需要增加：

```yaml
    ports:
      - "5256:5256"
      - "8011:8011"
```

## 本地源码运行

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

构建前端：

```bash
cd frontend
npm install
npm run build
```

启动主程序：

```bash
cd ..
python main.py
```

默认访问：

```text
http://127.0.0.1:5256
```

## 前端开发模式

```bash
cd frontend
npm install
npm run dev
```

前端开发服务器适合 UI 调试。后端接口仍由 `python main.py` 提供。

## NAS 直测部署

当需要把本地未提交改动直接部署到 NAS 测试时，优先使用仓库脚本：

```bash
scripts/deploy-nas-dev.sh
```

默认配置文件：

```text
scripts/deploy-nas-dev.env
```

默认 NAS 信息：

```text
Host: 192.168.2.2
SSH Port: 225
User: Chill
```

该流程会在本地构建测试镜像，通过局域网传输到 NAS，再由 NAS Docker Compose 重启服务。它适合快速验证本地改动，不等同于正式发布。

## 默认资源恢复

Docker 镜像内包含 `defaults/` 目录。启动时如果挂载目录缺少默认配置、字体、模板或布局文件，ChillPoster 会从默认副本恢复缺失文件，但不会覆盖用户已经存在的文件。

## 升级建议

1. 升级前备份 `config/`、`templates/`、`layouts/`、`fonts/` 与 `backups/`。
2. 拉取新镜像。
3. 重启容器。
4. 打开系统健康与任务中心，确认后台任务正常恢复。
5. 检查 `/api/version` 或 UI 显示版本。
