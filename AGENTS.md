# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project overview

ChillPoster is a Python/FastAPI application with a Vue/Vite management UI. The main app serves the management UI on port 5256 and starts one or more gateway/reverse-proxy servers for Emby-related traffic.

Primary entry points:

- `main.py` creates the FastAPI UI app, registers routers, mounts frontend static assets, starts schedulers/services during lifespan, exposes `/api/version`, and runs both UI and gateway uvicorn servers.
- `frontend/` is the browser UI source. It uses Vite with Vue and outputs `frontend/dist/` for local production serving.
- `static/` is kept as a legacy fallback/runtime static directory. Docker builds `frontend/` and copies the generated files into the runtime image's `/app/static`.
- `Dockerfile` builds the deployable image and injects `CHILLPOSTER_VERSION` from GitHub Actions build args.

## Architecture map

- `app/routers/` contains FastAPI route modules. `main.py` includes these routers for auth, server configuration, tasks, RSS, 302/gateway config, notifications, discovery, MoviePilot integration, resource transfer, STRM, Forward, and media organization.
- `app/services/` contains stateful service logic used by routers: 115 drive access, task scheduling, RSS, notifications, TMDB poster lookup, transfer, STRM, Forward, and media organization helpers.
- `core/` contains shared lower-level utilities and clients, including config loading, logging, Emby/TMDB/Douban helpers, media metadata parsing, organizer logic, and 115 life-event monitoring.
- `config/` stores local runtime configuration and logs. Most JSON files are intentionally ignored by Git; `config/media_organize_category_rules.json` is the tracked default rule file.
- `fonts/`, `layouts/`, and `templates/` are user-facing assets used by poster/template generation. `defaults/` contains default copies for Docker startup restore behavior.
- `MoviePilot-2.10.6/` and `MoviePilot-Frontend-2.10.5/` are external source trees kept locally but ignored by this repository; do not treat them as part of the root ChillPoster release unless explicitly asked.

## Common commands

Run from the repository root unless noted.

```bash
# Activate the local virtualenv if needed
source .venv/bin/activate

# Install/update Python dependencies
pip install -r requirements.txt

# Install/update frontend dependencies
cd frontend && npm install

# Run the frontend dev server
cd frontend && npm run dev

# Build the frontend
cd frontend && npm run build

# Run the app locally
python main.py

# Syntax-check the backend entrypoint
python -m py_compile main.py

# Validate the frontend bundle
cd frontend && npm run build

# Run the ad-hoc root test scripts if explicitly needed
python test_pickcode.py
python test_makedirs.py

# Build a local Docker image with an explicit version
 docker build --build-arg CHILLPOSTER_VERSION=v1.0.0.1 -t chillposter:test .

# Run the local Docker image
 docker run --rm -p 5256:5256 chillposter:test
```

The root `test_*.py` scripts are ad-hoc integration/debug scripts and may require local `config/config_302.json` credentials or 115 network access. There is no configured pytest suite at the root.

## NAS direct testing workflow

Only run the NAS direct testing workflow when the user explicitly asks to test on NAS, deploy to NAS, or "NAS 直测". Do not automatically deploy local changes to NAS after ordinary code edits or builds. When the user does explicitly ask for NAS direct testing, prefer the local LAN deployment flow instead of DockerHub:

- NAS host: `192.168.2.2`
- SSH port: `225`
- SSH user: `Chill`
- The NAS runs ChillPoster with Docker Compose.
- Use `scripts/deploy-nas-dev.env` for the local deployment configuration and `scripts/deploy-nas-dev.sh` for the workflow.
- Expected flow: build the local test image, save it to a local tar file, transfer the tar through the NAS SMB `docker` share, run `docker load -i` on the NAS, then restart the Compose service there.
- Always use SMB for the image tar transfer. Mount `smb://Chill@192.168.2.2/docker` locally and copy to `/Volumes/docker/ChillPoster/chillposter-nas-dev.tar`, which maps to `/vol2/1000/docker/ChillPoster/chillposter-nas-dev.tar` on the NAS.
- Do not use `scp`, `rsync` over SSH, raw SSH pipes, or ad-hoc TCP receivers for NAS direct testing unless the user explicitly asks for a different transfer path. In this LAN environment they have repeatedly stalled on large image tar transfers, while SMB transferred the 2.6 GB tar reliably in about 40 seconds.
- After a successful NAS-local `docker load -i` and Compose restart, verify `/api/version`, check `docker exec chillposter cat /app/VERSION`, and clean up the temporary tar file on both the local machine and the NAS.
- This path is for quick testing of local uncommitted changes. Do not use DockerHub unless the user explicitly asks for a release, DockerHub push, tag build, or formal publish.

## Versioning and release workflow

GitHub repository: `https://github.com/Chill-lucky/ChillPoster`

DockerHub image: `chillne/chillposter`

Release automation lives in `.github/workflows/docker-publish.yml`.

Version source of truth for Docker releases is the Git tag. Tags use the `vX.Y.Z.N` format, for example `v1.0.0.1`.

Release flow:

```bash
# If code changed, commit and push main first
git status
git add <changed-files>
git commit -m "Describe the change"
git push

# Publish a new Docker version
git tag v1.0.0.2
git push origin v1.0.0.2
```

Pushing a `v*` tag triggers GitHub Actions to build the root `Dockerfile` and push:

- `chillne/chillposter:v1.0.0.2`
- `chillne/chillposter:1.0.0.2`
- `chillne/chillposter:latest`

The workflow passes `CHILLPOSTER_VERSION=<tag>` as a Docker build arg. `main.py` reads `CHILLPOSTER_VERSION` first and falls back to `DEFAULT_PROJECT_VERSION`; the frontend reads `/api/version`, so do not manually edit frontend version display strings for releases.

GitHub Actions requires repository secrets:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

## Git and ignored local state

`.gitignore` intentionally excludes local/sensitive runtime state:

- `.venv/`, Python caches, `.env`, `.Codex/`, `.vscode/`
- `config/*.json` except `config/media_organize_category_rules.json`
- `config/*.log*`
- `backups/`
- `frontend/node_modules/`, `frontend/dist/`
- `MoviePilot-2.10.6/` and `MoviePilot-Frontend-2.10.5/`

Before committing, check `git status` and avoid staging ignored/generated local runtime files. Prefer staging specific files over broad adds when the changed set is unclear.

## Collaboration notes

The user prefers diagnosis and a clear plan before code changes. Do not modify code without explicit approval unless the user directly asks for the change. For release requests, verify `git status` first; uncommitted changes are not included in a tag-triggered Docker build unless they are committed and pushed before tagging.
