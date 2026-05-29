# Boot-Manager 项目上下文

本文档记录当前仓库的项目结构、核心链路和排查入口，方便后续新会话或新开发者快速接手。

## 项目概览

Boot-Manager 是一个轻量级 Docker Compose 启动管理器。它适合让其他 Docker Compose 项目保持停止状态，仅让 Boot-Manager 自身随 Docker 引擎 `always` 启动；Boot-Manager 启动后会先等待一个全局延迟，再按配置好的顺序和等待时间逐个执行：

```bash
docker compose -f <compose_path> up -d --wait
```

主要用途是让多组 Docker 服务错峰、优雅启动。

## 目录结构

```text
.
├── PROJECT_CONTEXT.md
├── 部署使用的docker-compose.yml
└── boot-manager
    ├── .dockerignore
    ├── Dockerfile
    ├── docker-compose.yml
    ├── index.html
    ├── main.py
    ├── requirements.txt
    └── static/vendor/vue.global.prod.js
```

当前没有测试目录、CI 配置、Makefile、`pyproject.toml` 或前端构建配置。

## 技术栈

- 后端：FastAPI，入口文件是 `boot-manager/main.py`。
- 前端：单文件 Vue 应用，入口文件是 `boot-manager/index.html`。
- 数据库：SQLite，默认路径是 `/app/data/data.db`。
- Docker 控制：容器内通过 Docker CLI 和挂载的 `/var/run/docker.sock` 控制宿主机 Docker。
- 前端依赖：本地 vendored Vue 文件 `boot-manager/static/vendor/vue.global.prod.js`。

## 核心配置

后端环境变量集中在 `boot-manager/main.py` 顶部：

- `ADMIN_KEY`：管理员密钥，必须配置；前端请求使用 `Authorization: Bearer <ADMIN_KEY>`。
- `BOOT_MANAGER_DB`：SQLite 数据库路径，默认 `/app/data/data.db`。
- `DOCKER_ROOT`：自动发现 Compose 项目的根目录，默认 `/docker`。
- `COMPOSE_UP_TIMEOUT_SECONDS`：单个项目 `docker compose up -d --wait` 的超时时间，默认 `600` 秒。
- `BOOT_ON_START_DELAY_SECONDS`：Boot-Manager 自身启动后，自动执行启动队列前等待的秒数，默认 `30` 秒。

部署相关文件：

- `boot-manager/docker-compose.yml`：开发或本地构建版本，使用 `build: .`，并强制要求 `ADMIN_KEY`。
- `部署使用的docker-compose.yml`：部署版本，使用镜像 `cakker/boot-manager:0.1.0`。
- `boot-manager/Dockerfile`：基于 `docker:29-cli`，安装 Python、Docker Compose CLI 和 Python 依赖。

关键挂载：

- `/var/run/docker.sock:/var/run/docker.sock`
- `/docker:/docker`
- `./data:/app/data`

## 后端核心模块

所有后端逻辑都在 `boot-manager/main.py` 中：

- 鉴权：`require_api_key()`。
- 数据库初始化：`init_db()`。
- 项目 CRUD：`insert_project()`、`update_project()`、`delete_project()`。
- 排序：`reorder_projects()`。
- 启动单个项目：`run_compose_project()`。
- 启动全部项目：`boot_all_projects()`。
- SSE 日志发布：`publish()` 和 `/api/events`。
- Docker 自动发现：`discover_existing_projects()`、`docker_event_listener()`、`discover_compose_path()`、`add_discovered_project()`。

SQLite 只有一张核心表 `projects`，字段包括：

- `id`
- `name`
- `compose_path`
- `delay_seconds`
- `sort_order`
- `needs_warning`
- `last_boot_time`

## 前端核心模块

前端全部在 `boot-manager/index.html`：

- 登录和密钥缓存：`login()`、`logout()`。
- 通用请求封装：`request()`。
- 项目列表刷新：`fetchProjects()`。
- SSE 实时日志：`connectEvents()`。
- Compose 路径补齐：`normalizeComposePath()`。
- 拖拽排序：`startProjectDrag()`、`dropProject()`、`saveProjectOrder()`。
- 项目保存：`saveProject()`。
- 一键启动：`bootAll()`。

前端会把管理密钥保存到浏览器 `localStorage` 的 `boot_manager_admin_key`。

## 自动启动链路

1. Docker 引擎启动后，只有 Boot-Manager 容器通过 `restart: always` 自动启动。
2. 应用启动时扫描 `DOCKER_ROOT` 及其一级子目录下的 Compose YAML，加入项目列表末尾。
3. 后端等待 `BOOT_ON_START_DELAY_SECONDS`。
4. 后端进入 `boot_all_projects()`。
5. `boot_all_projects()` 使用 `asyncio.Lock` 阻止并发启动。
6. 后端按 `sort_order ASC, id ASC` 读取项目。
7. 每个项目依次进入 `run_compose_project()`。
8. 项目先等待自己的 `delay_seconds`。
9. 后端执行 `docker compose -f <compose_path> up -d --wait`。
10. 成功后更新 `last_boot_time`。
11. 启动过程通过 `publish()` 推送到 `/api/events`。
12. 前端 `connectEvents()` 读取 SSE，把日志写入实时终端，并在 `done`、`boot_end`、`discovered` 时刷新项目列表。

## 手动启动链路

1. 用户点击前端“一键优雅启动”。
2. `index.html` 中的 `bootAll()` 设置 `booting = true`。
3. 前端调用 `POST /api/boot`。
4. 后端 `api_boot()` 进入 `boot_all_projects()`，后续流程与自动启动链路一致。
## 自动发现链路

应用启动时，`lifespan()` 会先执行一次目录扫描，再启动后台线程 `docker_event_listener()`。

扫描逻辑：

1. 遍历 `DOCKER_ROOT` 和它的一级子目录。
2. 查找：
   - `docker-compose.yml`
   - `docker-compose.yaml`
   - `compose.yml`
   - `compose.yaml`
3. 找到后写入 `projects` 表，并标记 `needs_warning = true`。
4. 通过 SSE 通知前端“自动发现新项目”。

监听逻辑：

1. 通过 `docker.from_env()` 连接 Docker。
2. 监听容器 `start` 事件。
3. 读取事件里的 `com.docker.compose.project` 标签。
4. 优先使用事件里的真实 Compose 配置文件路径或工作目录；如果没有，再在 `DOCKER_ROOT/<project_name>/` 下查找：
   - `docker-compose.yml`
   - `docker-compose.yaml`
   - `compose.yml`
   - `compose.yaml`
5. 找到后写入 `projects` 表，并标记 `needs_warning = true`。
6. 通过 SSE 通知前端“自动发现新项目”。

注意：启动扫描依赖 Compose YAML 位于 `DOCKER_ROOT` 或其一级子目录；Docker 事件发现会优先使用 Compose 标签里的真实路径。

## 常见排查入口

- 启动失败、超时、启动顺序异常：优先看 `run_compose_project()` 和 `boot_all_projects()`。
- 页面按钮一直显示启动中：看前端 `bootAll()`、`connectEvents()`，以及后端是否一定推送 `boot_end`。
- 实时日志不更新：看 `/api/events`、`publish()`、前端 `connectEvents()`。
- 登录失败：看 `ADMIN_KEY`、请求头 `Authorization`、`require_api_key()`。
- 项目新增或编辑失败：看 `ProjectCreate`、`ProjectUpdate`、`insert_project()`、`update_project()`。
- 拖拽排序失败：看前端 `saveProjectOrder()` 和后端 `reorder_projects()`。
- 自动发现失败：看 `DOCKER_ROOT`、Docker socket 挂载、Compose project 标签和 `discover_compose_path()`。
- 数据不持久：看 `./data:/app/data` 挂载和 `BOOT_MANAGER_DB`。
- 部署后 Docker 命令不可用：看镜像内 Docker CLI、Compose CLI 和 `/var/run/docker.sock` 挂载。

## 已知验证边界

本次项目理解未做实际部署测试，也未执行 `docker compose up`。已做过的轻量检查包括：

- 阅读全部项目文件。
- 静态解析两个 Compose 文件。
- 对 `boot-manager/main.py` 做 Python 语法级检查。

后续修复 bug 时，优先采用最小验证：

```bash
PYTHONPYCACHEPREFIX=/tmp/boot-manager-pycache python3 -m py_compile boot-manager/main.py
```

如需验证 API，建议在隔离环境使用临时数据库和临时 `DOCKER_ROOT`：

```bash
cd boot-manager
ADMIN_KEY=dev BOOT_MANAGER_DB=/tmp/boot-manager.db DOCKER_ROOT=/tmp/docker uvicorn main:app --host 127.0.0.1 --port 8000
```

除非明确需要，不要直接调用生产项目的 `/api/boot`，因为它会执行真实 Docker Compose 启动。

## 协作约定

- 交互、说明和文档使用中文。
- 修改要尽量小，只触碰和 bug 直接相关的文件。
- 不要做无关重构。
- 修复 bug 时先把现象映射到具体链路，再写最小补丁。
- 没有测试框架时，至少做语法检查、静态配置检查或针对性 API 烟测。
