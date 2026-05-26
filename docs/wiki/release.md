# 发布流程

ChillPoster 的正式 Docker 版本以 Git tag 为准。发布前需要确保代码已经提交并推送到主分支。

## 版本规则

Tag 使用：

```text
vX.Y.Z.N
```

示例：

```text
v1.0.0.2
```

## 发布前检查

```bash
git status
```

确认没有遗漏改动。未提交的本地改动不会进入 GitHub Actions 构建。

建议至少验证：

```bash
python -m py_compile main.py
```

如涉及前端：

```bash
cd frontend
npm run build
```

## 提交并推送代码

```bash
git add <changed-files>
git commit -m "Describe the change"
git push
```

不要使用宽泛的 `git add .`，除非已经确认没有本地配置、缓存、日志或构建产物会被误提交。

## 创建版本标签

```bash
git tag v1.0.0.2
git push origin v1.0.0.2
```

推送 `v*` tag 后，GitHub Actions 会执行 `.github/workflows/docker-publish.yml`。

## 镜像标签

发布成功后会推送：

```text
chillne/chillposter:v1.0.0.2
chillne/chillposter:1.0.0.2
chillne/chillposter:latest
```

## 版本注入

GitHub Actions 会把 tag 作为 Docker build arg 注入：

```text
CHILLPOSTER_VERSION=<tag>
```

后端版本读取顺序：

1. `VERSION` 文件
2. `CHILLPOSTER_VERSION` 环境变量
3. `DEFAULT_PROJECT_VERSION`

前端通过 `/api/version` 获取版本，不需要手动改前端版本显示。

## DockerHub Secrets

GitHub Actions 需要仓库配置：

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

## NAS 测试与正式发布的区别

NAS 直测用于验证本地未提交改动：

```bash
scripts/deploy-nas-dev.sh
```

正式发布用于给所有用户分发版本：

```bash
git tag vX.Y.Z.N
git push origin vX.Y.Z.N
```

不要把 NAS 直测当成正式发布，也不要在没有明确发布需求时推送 DockerHub。

## 回滚建议

如果新版本出现问题：

1. 在 NAS 或服务器上切回上一个稳定镜像标签。
2. 保留当前 `config/` 备份。
3. 查看 `config/app.log` 与 GitHub Actions 日志定位原因。
4. 修复后发布新的递增版本 tag，不要覆盖已经发布的 tag。
