# 前端阶段：压缩静态脚本，降低浏览器端代码可读性
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend

WORKDIR /src
COPY static ./static
RUN mkdir -p /protected-static \
    && cp -a static/. /protected-static/ \
    && npx --yes terser@5 static/app.js --compress --mangle --comments false --output /protected-static/app.js

# 构建阶段：按目标架构编译 Python 扩展模块，避免多架构镜像混用二进制
FROM python:3.12-slim AS builder

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 -r requirements.txt \
    && pip install --no-cache-dir --default-timeout=300 nuitka ordered-set zstandard

COPY . .
COPY config/media_organize_category_rules.json config/media_organize_category_rules.json

RUN python - <<'PY'
from pathlib import Path
import os
import py_compile
import shutil
import subprocess
import sys
import sysconfig

src = Path('/src')
out = Path('/protected')
work_root = Path('/tmp/nuitka-build')

if out.exists():
    shutil.rmtree(out)
if work_root.exists():
    shutil.rmtree(work_root)
out.mkdir(parents=True)
work_root.mkdir(parents=True)

# 让 Nuitka 明确按包模块处理 app/core 下的文件；这些标记文件不包含业务逻辑。
for marker in [
    'app/__init__.py',
    'app/routers/__init__.py',
    'app/services/__init__.py',
    'app/discover_plugins/__init__.py',
    'core/__init__.py',
]:
    path = src / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('', encoding='utf-8')

ext_suffix = sysconfig.get_config_var('EXT_SUFFIX') or '.so'


def compile_extension(path: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    work = work_root / str(target.with_suffix('').relative_to(out)).replace(os.sep, '__')
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    try:
        module_input = str(path.relative_to(src))
    except ValueError:
        module_input = str(path)

    cmd = [
        sys.executable,
        '-m',
        'nuitka',
        '--module',
        '--quiet',
        '--no-pyi-file',
        '--remove-output',
        f'--output-dir={work}',
        module_input,
    ]
    subprocess.run(cmd, cwd=str(src), check=True)

    candidates = list(work.rglob(f'{path.stem}*.so')) + list(path.parent.glob(f'{path.stem}*.so'))
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if not candidates:
        raise RuntimeError(f'Nuitka did not produce an extension for {path}')
    produced = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    if target.exists():
        target.unlink()
    shutil.move(str(produced), str(target))

    for leftover in path.parent.glob(f'{path.stem}*.build'):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
    for leftover in path.parent.glob(f'{path.stem}*.pyi'):
        leftover.unlink(missing_ok=True)


def compile_sourceless(path: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(path), cfile=str(target), doraise=True)


def copy_file(path: Path, target: Path):
    if path.name == '.DS_Store' or '__pycache__' in path.parts:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


def protect_python_file(path: Path, rel: Path):
    target_dir = out / rel.parent
    if path.name == '__init__.py':
        text = path.read_text(encoding='utf-8', errors='ignore')
        if text.strip():
            compile_sourceless(path, target_dir / '__init__.pyc')
        else:
            (target_dir / '__init__.py').parent.mkdir(parents=True, exist_ok=True)
            (target_dir / '__init__.py').write_text('', encoding='utf-8')
        return

    text = path.read_text(encoding='utf-8', errors='ignore')

    # 这些目录由运行时 importlib 按文件路径扫描，保留为无源码 pyc 以兼容现有加载器。
    if rel.parts[:2] == ('app', 'discover_plugins') or rel.parts[:1] == ('layouts',):
        compile_sourceless(path, out / rel.with_suffix('.pyc'))
        return

    # FastAPI/Pydantic 依赖函数签名和类型注解；接口层先用 sourceless pyc 保证注册稳定。
    if rel.parts[:2] == ('app', 'routers') or rel in {Path('app/dependencies.py'), Path('app/schemas.py')}:
        compile_sourceless(path, out / rel.with_suffix('.pyc'))
        return

    # 单文件扩展模块和显式相对导入组合容易受包名影响；这类少数模块保守用 sourceless pyc。
    if '\nfrom .' in f'\n{text}' or '\nimport .' in f'\n{text}':
        compile_sourceless(path, out / rel.with_suffix('.pyc'))
        return

    compile_extension(path, out / rel.with_suffix(ext_suffix))


# 用户可见资源和默认文件。
for name in ['templates', 'fonts']:
    path = src / name
    if path.exists():
        shutil.copytree(path, out / name)

for name in ['static', 'config', 'backups', 'layouts', 'defaults']:
    (out / name).mkdir(parents=True, exist_ok=True)

rules = src / 'config' / 'media_organize_category_rules.json'
if rules.exists():
    (out / 'config').mkdir(parents=True, exist_ok=True)
    shutil.copy2(rules, out / 'config' / rules.name)

requirements = src / 'requirements.txt'
if requirements.exists():
    shutil.copy2(requirements, out / 'requirements.txt')

# 主入口改名后编译，最终只暴露一个极小的 sourceless 启动器。
main_copy = Path('/tmp/chillposter_main.py')
shutil.copy2(src / 'main.py', main_copy)
compile_extension(main_copy, out / f'chillposter_main{ext_suffix}')

launcher = """import asyncio\nimport os\nimport chillposter_main as app_main\n\nif __name__ == \"__main__\":\n    app_main.restore_defaults()\n\n    if not os.path.exists(\"fonts\"):\n        os.makedirs(\"fonts\")\n        app_main.logger.info(\"创建 fonts 目录\")\n\n    app_main.logger.info(\"[启动] 正在启动服务\")\n    app_main.logger.info(\"[启动] 管理后台: http://localhost:5256/static/index.html\")\n\n    try:\n        asyncio.run(app_main.serve_apps())\n    except KeyboardInterrupt:\n        app_main.logger.warning(\"[启动] 服务已停止\")\n    except Exception as e:\n        app_main.logger.error(f\"[启动] 服务异常退出: {e}\")\n"""
launcher_path = Path('/tmp/chillposter_launcher.py')
launcher_path.write_text(launcher, encoding='utf-8')
compile_sourceless(launcher_path, out / 'main.pyc')

for folder in ['app', 'core']:
    base = src / folder
    if not base.exists():
        continue
    for path in base.rglob('*'):
        rel = path.relative_to(src)
        if path.is_dir():
            (out / rel).mkdir(parents=True, exist_ok=True)
        elif path.suffix == '.py':
            protect_python_file(path, rel)
        else:
            copy_file(path, out / rel)

for name in ['client.py', 'config_manager.py', 'constants.py', 'utils.py']:
    path = src / name
    if path.exists():
        compile_extension(path, out / path.with_suffix(ext_suffix).name)

layouts = src / 'layouts'
if layouts.exists():
    for path in layouts.iterdir():
        if path.is_file() and path.name != '__init__.py':
            rel = Path('layouts') / path.name
            if path.suffix == '.py':
                protect_python_file(path, rel)
            else:
                copy_file(path, out / rel)

for folder in ['config', 'templates', 'layouts', 'fonts']:
    source = out / folder
    target = out / 'defaults' / folder
    target.mkdir(parents=True, exist_ok=True)
    if source.exists():
        for item in source.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
PY

RUN cp -a /protected /tmp/protected-smoke && cd /tmp/protected-smoke && python - <<'PY'
import chillposter_main as app_main
from core.engine import PosterEngine

app_main.app.openapi()
app_main.proxy_app.openapi()
PosterEngine(fonts_dir="fonts", layouts_dir="layouts")
print("protected runtime smoke ok")
PY

# 运行阶段：只复制编译后的代码与资源，不包含源码层
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
ARG CHILLPOSTER_VERSION=vdev
ARG BUILD_DATE
LABEL org.opencontainers.image.version=$CHILLPOSTER_VERSION
LABEL org.opencontainers.image.created=$BUILD_DATE
RUN echo "$CHILLPOSTER_VERSION" > /app/VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libgcc-s1 \
    libstdc++6 \
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

COPY --from=builder /protected/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 -r requirements.txt
RUN playwright install --with-deps chromium

COPY --from=builder /protected/ .
COPY --from=frontend /protected-static/ static/

EXPOSE 5256
VOLUME ["/app/config", "/app/templates", "/app/layouts", "/app/fonts", "/app/backups"]
CMD ["python", "main.pyc"]
