# Boot-Manager

Boot-Manager 是一个轻量级 Docker Compose 启动管理器，用来让多组 Compose 项目按顺序、带延迟地错峰启动。

适合的部署方式是：其他 Compose 项目关闭 Docker 自启动，只让 Boot-Manager 自身随 Docker 引擎启动。Boot-Manager 启动后会等待一段时间，再按配置顺序执行：

```bash
docker compose -f <compose_path> up -d --wait
```

## 功能

- Web 管理界面，使用 `ADMIN_KEY` 登录。
- 添加、编辑、删除 Compose 项目。
- 支持拖拽调整启动顺序。
- 支持为每个项目设置启动前等待秒数。
- 启动过程通过实时日志显示。
- 启动时扫描 `/docker` 及一级子目录中的 Compose 文件。
- 监听 Docker 容器启动事件，自动发现新的 Compose 项目。

## 快速部署

推荐使用已经发布的镜像：

```yaml
services:
  boot-manager:
    image: cakker/boot-manager:0.1.1
    container_name: boot-manager
    ports:
      - "8080:8000"
    environment:
      - ADMIN_KEY=${ADMIN_KEY:-change-me-before-deploy}
      - BOOT_ON_START_DELAY_SECONDS=15
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /docker:/docker
      - ./data:/app/data
    restart: always
```

启动：

```bash
docker compose up -d
```

然后访问：

```text
http://<你的服务器 IP>:8080
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ADMIN_KEY` | 是 | 无 | 管理员登录密钥。 |
| `BOOT_MANAGER_DB` | 否 | `/app/data/data.db` | SQLite 数据库路径。 |
| `DOCKER_ROOT` | 否 | `/docker` | 扫描 Compose 项目的根目录。 |
| `BOOT_ON_START_DELAY_SECONDS` | 否 | `30` | Boot-Manager 启动后，等待多少秒再执行启动队列。 |
| `COMPOSE_UP_TIMEOUT_SECONDS` | 否 | `600` | 单个项目执行 `docker compose up -d --wait` 的超时时间。 |

## 本地构建

```bash
cd boot-manager
docker build -t boot-manager:local .
```

本地 Compose 启动：

```bash
cd boot-manager
ADMIN_KEY=dev docker compose up -d
```

## 注意事项

- 容器需要挂载 `/var/run/docker.sock`，因为它会调用宿主机 Docker。
- 被管理的 Compose 项目建议不要再配置 Docker 自动重启启动链路，否则可能和 Boot-Manager 的错峰启动冲突。
- `./data` 保存数据库，建议保留持久化挂载。
- 不要把 `.env`、数据目录或本机私有部署文件提交到仓库。
