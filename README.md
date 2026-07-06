# ChillPoster

ChillPoster 是面向 Emby 和网盘生态的家庭影音自动化中枢。项目后端使用 Python/FastAPI，前端使用 Vue/Vite，Dockerfile 会先构建前端，再打包后端运行环境。默认服务端口为 `5256`。

## 当前已有功能

- Emby 管理：服务器配置、媒体库封面、自动封面、封面备份、模板和字体管理。
- 115 云盘一条龙：115 Cookie/扫码登录、302 播放网关、同播复制、秒传小号、资源转存、媒体整理、定时清空、秒传/上传监听。
- CloudDrive2 云盘接入：通过 CloudDrive2 WebDAV 接入 123 云盘和光鸭云盘，复用浏览、标准目录、媒体整理、STRM、上传、清理和 302 播放链路。
- 媒体整理：TMDb 识别、电影/剧集命名模板、二级分类、洗版判断、整理历史、监控目录和整理后 STRM。
- STRM 同步：115 全量/增量同步，CloudDrive2 全量/增量同步，附属字幕/图片/数据文件下载。
- 发现与订阅：影视发现、缺集统计、RSS 真实库、独立真实库、MoviePilot 联动。
- 通知和运维：微信、Telegram、Webhook、任务中心、Docker 管理、健康检查、系统升级。

## 镜像地址

```text
ghcr.io/zhang6743779-debug/chillposter:latest
```

GitHub Actions 会在推送到 `main` 或推送 `v*` 标签时自动构建并推送多架构镜像到 GitHub Container Registry。

## 默认账号密码

首次启动且没有挂载旧的 `config/auth.json` 时，默认登录信息为：

```text
用户名：admin
密码：password
```

也可以通过环境变量 `CHILLPOSTER_ADMIN_USERNAME` 和 `CHILLPOSTER_ADMIN_PASSWORD` 修改首次默认账号密码。进入系统后，在右上角账号面板里修改账号密码会写入 `config/auth.json`，后续优先使用已保存的账号密码。

## 飞牛 Docker/容器管理器部署

### 方式一：使用飞牛 Compose 项目

1. 打开飞牛 `Docker` 或 `容器管理器`。
2. 进入 `Compose` / `项目` / `创建项目`。
3. 项目名称填写 `chillposter`。
4. 粘贴下面的 `docker-compose.yml`。
5. 部署后访问 `http://飞牛IP:5256/static/index.html`。

飞牛可直接复制的 compose 文件：

```yaml
services:
  chillposter:
    image: ghcr.io/zhang6743779-debug/chillposter:latest
    container_name: chillposter
    ports:
      - "5256:5256"
    environment:
      TZ: Asia/Shanghai
      CHILLPOSTER_IMAGE: ghcr.io/zhang6743779-debug/chillposter:latest
      CHILLPOSTER_ADMIN_USERNAME: admin
      CHILLPOSTER_ADMIN_PASSWORD: password
    volumes:
      - ./config:/app/config
      - ./fonts:/app/fonts
      - ./templates:/app/templates
      - ./layouts:/app/layouts
      - ./backups:/app/backups
    restart: unless-stopped
```

### 方式二：使用飞牛图形化创建容器

1. 镜像填写 `ghcr.io/zhang6743779-debug/chillposter:latest`。
2. 容器名称填写 `chillposter`。
3. 端口映射填写 `5256 -> 5256`。
4. 添加数据卷映射：
   - `./config` -> `/app/config`
   - `./fonts` -> `/app/fonts`
   - `./templates` -> `/app/templates`
   - `./layouts` -> `/app/layouts`
   - `./backups` -> `/app/backups`
5. 添加环境变量：
   - `TZ=Asia/Shanghai`
   - `CHILLPOSTER_IMAGE=ghcr.io/zhang6743779-debug/chillposter:latest`
   - `CHILLPOSTER_ADMIN_USERNAME=admin`
   - `CHILLPOSTER_ADMIN_PASSWORD=password`
6. 重启策略选择 `unless-stopped` 或 `总是重启`。
7. 启动容器后访问 `http://飞牛IP:5256/static/index.html`。

如果飞牛拉取 GHCR 镜像失败，请确认飞牛主机可以访问 `ghcr.io`，并确认 GitHub Packages 中该镜像已设置为公开可见。

## 端口映射

| 宿主机端口 | 容器端口 | 说明 |
| --- | --- | --- |
| `5256` | `5256` | ChillPoster Web 管理界面和 API |

访问地址：

```text
http://你的服务器IP:5256/static/index.html
```

## 数据卷映射

| 宿主机目录 | 容器目录 | 说明 |
| --- | --- | --- |
| `./config` | `/app/config` | 配置、账号、任务状态、缓存数据库 |
| `./fonts` | `/app/fonts` | 字体文件 |
| `./templates` | `/app/templates` | 海报模板 |
| `./layouts` | `/app/layouts` | 布局文件 |
| `./backups` | `/app/backups` | 备份文件 |

升级镜像前建议至少备份 `config` 目录。

## 环境变量说明

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `CHILLPOSTER_IMAGE` | `ghcr.io/zhang6743779-debug/chillposter:latest` | 当前部署镜像地址，供升级/运维页面识别 |
| `CHILLPOSTER_ADMIN_USERNAME` | `admin` | 首次启动默认用户名，仅在 `config/auth.json` 不存在时生效 |
| `CHILLPOSTER_ADMIN_PASSWORD` | `password` | 首次启动默认密码，仅在 `config/auth.json` 不存在时生效 |
| `CHILLPOSTER_LOCAL_MEDIA_ROOT` | 容器内用户桌面路径 | 一条龙标准本地媒体根目录，可按需改为挂载目录 |
| `CHILLPOSTER_STRM_HOST` | 空 | STRM 生成时的外部访问地址，留空时使用界面配置 |
| `CHILLPOSTER_PROXY_URL` | 空 | 部分外部接口请求代理，按需填写 |

## CloudDrive2 接入 123 云盘和光鸭云盘

1. 先在 CloudDrive2 中添加 123 云盘或光鸭云盘账号，并开启 WebDAV 服务。
2. 在 ChillPoster 进入 `云盘配置`，云盘类型选择 `123 云盘（CloudDrive2）` 或 `光鸭云盘（CloudDrive2，只读）`。
3. 填写 CloudDrive2 WebDAV 地址，例如 `http://clouddrive2:19798/dav` 或反代后的 HTTPS 地址。
4. 如 CloudDrive2 WebDAV 开启了鉴权，填写用户名和密码。
5. `云盘根路径` 用于限定 ChillPoster 可见的根目录，默认 `/`。
6. `播放直链基础地址` 可选。留空时会使用 WebDAV 地址生成播放直链；如果 Emby 客户端访问不到内网地址，建议填反代后的外网地址。

123 云盘在 CloudDrive2 WebDAV 下按可写云盘处理，支持浏览、创建标准目录、上传、清理、媒体整理、STRM 和 302 播放。光鸭云盘按只读云盘处理，保存配置时只验证 CloudDrive2 根目录可访问，不会自动创建标准目录；支持浏览、STRM 和直链播放，创建目录、上传、删除、移动、媒体整理等写操作会被 ChillPoster 拦截并提示只读。

## 普通服务器 docker compose 部署

```bash
mkdir -p chillposter
cd chillposter
mkdir -p config fonts templates layouts backups
```

保存 `docker-compose.yml` 后启动：

```bash
docker compose up -d
docker compose logs -f
```

更新镜像：

```bash
docker compose pull
docker compose up -d
```

停止服务：

```bash
docker compose down
```

## 源码构建

项目已有 Dockerfile，GitHub Actions 和本地构建都使用该 Dockerfile。

```bash
docker build --build-arg CHILLPOSTER_VERSION=vdev -t chillposter:local .
docker run -d --name chillposter -p 5256:5256 chillposter:local
```

## 本地开发

```bash
pip install -r requirements.txt

cd frontend
npm ci
npm run build

cd ..
python main.py
```

## 核心接口路径

- UI 与基础：`/static/index.html`、`/api/version`、`/api/login`、`/api/load`、`/api/save`
- Emby/封面：`/api/connect`、`/api/library_covers`、`/api/templates_v2`、`/api/apply`
- 云盘配置：`/api/config_302/get`、`/api/config_302/save`、`/api/config_302/test_115`、`/api/config_302/test_cloud_drive`
- CloudDrive2：`/api/cloud_drive/test`、`/api/cloud_drive/browse`
- 115 扫码：`/api/config_302/115_qrcode/start`、`/api/config_302/115_qrcode/status`、`/api/config_302/115_qrcode/result`
- 媒体整理：`/api/media_organize/get`、`/api/media_organize/save`、`/api/media_organize/browse115`、`/api/media_organize/organize`
- STRM：`/api/strm/get`、`/api/strm/save`、`/api/strm/start`、`/api/strm/stop`
- 115 工具：`/api/drive115_cleanup/*`、`/api/drive115_upload/*`
- 302 网关：`/d/{pickcode}.ext` 用于 115，`/cd/{drive_index}/{encoded_path}.ext` 用于 CloudDrive2

## 测试命令

```bash
python -m py_compile main.py app/routers/cloud_drive.py app/services/cloud_drive_provider.py app/routers/config_302.py app/services/media_organize_core.py app/services/strm_service.py

cd frontend
npm ci
npm run build

cd ..
docker build --build-arg CHILLPOSTER_VERSION=vdev -t chillposter:local .
docker compose up -d
curl http://127.0.0.1:5256/api/version
```

CloudDrive2 连接测试：

```bash
curl -X POST http://127.0.0.1:5256/api/config_302/test_cloud_drive \
  -H "Content-Type: application/json" \
  -d '{"provider":"123pan","clouddrive_base_url":"http://127.0.0.1:19798/dav","clouddrive_username":"","clouddrive_password":"","clouddrive_root_path":"/"}'
```

## 项目结构

```text
ChillPoster/
  main.py                         FastAPI 主入口，注册 UI API 与 302 网关应用
  Dockerfile                      多阶段镜像构建，前端 Vite + 后端运行环境
  docker-compose.yml              GHCR 镜像部署模板
  requirements.txt                Python 依赖
  app/
    routers/                      FastAPI 路由
    services/                     业务服务层
    discover_plugins/             发现推荐插件
  core/                           Emby、TMDb、媒体识别、缓存、115 monitor 等核心模块
  frontend/
    index.html                    Vue 单页管理界面模板
    src/                          Vue/Vite 前端逻辑
    package.json                  前端依赖与构建命令
  config/                         运行配置
  fonts/ templates/ layouts/      字体、模板、布局资源
  defaults/                       默认资源备份
  docs/                           文档和截图
```

## 注意事项

- 原有 115 核心功能没有删除；CloudDrive2 是在 provider 分支上新增的适配路径。
- CloudDrive2 直链如果包含 WebDAV Basic Auth，部分 Emby 客户端可能不接受带用户名密码的 URL。遇到这种情况请配置 `播放直链基础地址` 为可访问的反代地址。
- 光鸭云盘在 CloudDrive2 当前适配下按只读处理，不能承诺与 115/123 一样执行写入类操作。
