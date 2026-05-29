import asyncio
import hmac
import json
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from contextlib import closing
from pathlib import Path
from typing import Any

import docker
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


DB_PATH = os.getenv("BOOT_MANAGER_DB", "/app/data/data.db")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
INDEX_PATH = Path(__file__).with_name("index.html")
STATIC_ROOT = Path(__file__).with_name("static").resolve()
DOCKER_ROOT = Path(os.getenv("DOCKER_ROOT", "/docker"))
DEFAULT_COMPOSE_UP_TIMEOUT_SECONDS = 600
DEFAULT_BOOT_ON_START_DELAY_SECONDS = 30
COMPOSE_FILE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

boot_lock = asyncio.Lock()
subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
main_loop: asyncio.AbstractEventLoop | None = None


def require_configured_admin_key() -> str:
    if not ADMIN_KEY.strip():
        raise RuntimeError("ADMIN_KEY environment variable is required")
    return ADMIN_KEY


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    configured_key = require_configured_admin_key()
    prefix = "Bearer "
    supplied_key = ""
    if authorization and authorization.startswith(prefix):
        supplied_key = authorization[len(prefix):]
    if not hmac.compare_digest(supplied_key, configured_key):
        raise HTTPException(status_code=401, detail="invalid admin key")


def require_event_stream_key(authorization: str | None = Header(default=None)) -> None:
    require_api_key(authorization)


def get_compose_up_timeout() -> int:
    raw_timeout = os.getenv("COMPOSE_UP_TIMEOUT_SECONDS", str(DEFAULT_COMPOSE_UP_TIMEOUT_SECONDS))
    try:
        timeout = int(raw_timeout)
    except ValueError:
        return DEFAULT_COMPOSE_UP_TIMEOUT_SECONDS
    return max(1, timeout)


def get_boot_on_start_delay() -> int:
    raw_delay = os.getenv("BOOT_ON_START_DELAY_SECONDS", str(DEFAULT_BOOT_ON_START_DELAY_SECONDS))
    try:
        delay = int(raw_delay)
    except ValueError:
        return DEFAULT_BOOT_ON_START_DELAY_SECONDS
    return max(0, delay)


def get_db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                compose_path TEXT NOT NULL UNIQUE,
                delay_seconds INTEGER NOT NULL DEFAULT 3,
                sort_order INTEGER NOT NULL DEFAULT 0,
                needs_warning BOOLEAN NOT NULL DEFAULT 0,
                last_boot_time FLOAT NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_sort ON projects(sort_order, id)")


def row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    project = dict(row)
    project["needs_warning"] = bool(project["needs_warning"])
    return project


def list_projects() -> list[dict[str, Any]]:
    with closing(get_db()) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [row_to_project(row) for row in rows]


def get_project(project_id: int) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row_to_project(row) if row else None


def next_sort_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM projects").fetchone()
    return int(row["next_order"])


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    compose_path: str = Field(..., min_length=1)
    delay_seconds: int = Field(3, ge=0)
    sort_order: int | None = Field(None, ge=0)
    needs_warning: bool = False


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1)
    compose_path: str | None = Field(None, min_length=1)
    delay_seconds: int | None = Field(None, ge=0)
    sort_order: int | None = Field(None, ge=0)
    needs_warning: bool | None = None


class ProjectReorder(BaseModel):
    project_ids: list[int] = Field(..., min_length=1)


def insert_project(payload: ProjectCreate) -> dict[str, Any]:
    with closing(get_db()) as conn, conn:
        sort_order = payload.sort_order if payload.sort_order is not None else next_sort_order(conn)
        try:
            cur = conn.execute(
                """
                INSERT INTO projects
                    (name, compose_path, delay_seconds, sort_order, needs_warning, last_boot_time)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    payload.name.strip(),
                    payload.compose_path.strip(),
                    payload.delay_seconds,
                    sort_order,
                    int(payload.needs_warning),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="compose_path already exists") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_project(row)


def update_project(project_id: int, payload: ProjectUpdate) -> dict[str, Any]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        project = get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    assignments = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(int(value) if key == "needs_warning" else value)
    values.append(project_id)

    with closing(get_db()) as conn, conn:
        try:
            cur = conn.execute(
                f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="compose_path already exists") from exc
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="project not found")
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row_to_project(row)


def delete_project(project_id: int) -> None:
    with closing(get_db()) as conn, conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="project not found")


def reorder_projects(payload: ProjectReorder) -> list[dict[str, Any]]:
    ordered_ids = payload.project_ids
    if len(set(ordered_ids)) != len(ordered_ids):
        raise HTTPException(status_code=400, detail="project_ids must be unique")

    with closing(get_db()) as conn, conn:
        rows = conn.execute("SELECT id FROM projects").fetchall()
        existing_ids = {int(row["id"]) for row in rows}
        requested_ids = set(ordered_ids)
        if requested_ids != existing_ids:
            raise HTTPException(status_code=400, detail="project_ids must include every project exactly once")

        for sort_order, project_id in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE projects SET sort_order = ? WHERE id = ?",
                (sort_order, project_id),
            )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM projects ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [row_to_project(row) for row in rows]


async def publish(event: str, message: str, project: dict[str, Any] | None = None) -> None:
    payload = {
        "event": event,
        "message": message,
        "project": project,
        "timestamp": time.time(),
    }
    for queue in list(subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            subscribers.discard(queue)


def update_last_boot_time(project_id: int, elapsed: float) -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(
            "UPDATE projects SET last_boot_time = ? WHERE id = ?",
            (elapsed, project_id),
        )
        conn.commit()


async def run_compose_project(project: dict[str, Any]) -> None:
    delay = int(project["delay_seconds"])
    if delay > 0:
        await publish("waiting", f"{project['name']} 启动前等待 {delay} 秒", project)
        await asyncio.sleep(delay)

    await publish("starting", f"{project['name']} 正在执行 docker compose up -d --wait", project)
    started = time.perf_counter()
    command = ["docker", "compose", "-f", project["compose_path"], "up", "-d", "--wait"]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=get_compose_up_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        await publish("error", f"{project['name']} 启动超时，请检查 compose 健康检查或调大超时", project)
        raise HTTPException(status_code=504, detail="docker compose timed out") from exc
    except FileNotFoundError as exc:
        await publish("error", "容器内未找到 docker CLI，请确认镜像内已安装 docker-cli", project)
        raise HTTPException(status_code=500, detail="docker CLI not found") from exc

    elapsed = round(time.perf_counter() - started, 2)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker compose failed").strip()
        await publish("error", f"{project['name']} 启动失败：{detail}", project)
        raise HTTPException(status_code=500, detail=detail)

    update_last_boot_time(int(project["id"]), elapsed)
    await publish("done", f"{project['name']} 启动完成，耗时 {elapsed:.2f} 秒", {**project, "last_boot_time": elapsed})


async def boot_all_projects() -> dict[str, Any]:
    if boot_lock.locked():
        raise HTTPException(status_code=409, detail="boot is already running")

    async with boot_lock:
        projects = list_projects()
        await publish("boot_start", f"开始错峰启动，共 {len(projects)} 个项目")
        completed = 0
        try:
            for project in projects:
                await run_compose_project(project)
                completed += 1
        finally:
            await publish("boot_end", f"错峰启动结束，完成 {completed}/{len(projects)} 个项目")
    return {"ok": True, "completed": completed, "total": len(projects)}


def discover_compose_path(project_name: str, attrs: dict[str, Any] | None = None) -> Path | None:
    if attrs:
        config_files = attrs.get("com.docker.compose.project.config_files", "")
        for config_file in str(config_files).split(","):
            if not config_file.strip():
                continue
            compose_path = Path(config_file.strip())
            if compose_path.exists():
                return compose_path

        working_dir = attrs.get("com.docker.compose.project.working_dir")
        if working_dir:
            compose_path = discover_compose_in_directory(Path(str(working_dir)))
            if compose_path:
                return compose_path

    candidates = [DOCKER_ROOT / project_name / file_name for file_name in COMPOSE_FILE_NAMES]
    return next((path for path in candidates if path.exists()), None)


def discover_compose_in_directory(directory: Path) -> Path | None:
    candidates = [directory / file_name for file_name in COMPOSE_FILE_NAMES]
    return next((path for path in candidates if path.exists()), None)


def discover_existing_compose_paths() -> list[Path]:
    if not DOCKER_ROOT.exists():
        return []

    directories = [DOCKER_ROOT]
    directories.extend(path for path in DOCKER_ROOT.iterdir() if path.is_dir())

    compose_paths: list[Path] = []
    for directory in directories:
        compose_path = discover_compose_in_directory(directory)
        if compose_path:
            compose_paths.append(compose_path)
    return sorted(compose_paths)


def project_exists(project_name: str, compose_path: Path) -> bool:
    with closing(get_db()) as conn, conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = ? OR compose_path = ?",
            (project_name, str(compose_path)),
        ).fetchone()
    return row is not None


def add_discovered_project(project_name: str, compose_path: Path) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        if project_exists(project_name, compose_path):
            return None
        cur = conn.execute(
            """
            INSERT INTO projects
                (name, compose_path, delay_seconds, sort_order, needs_warning, last_boot_time)
            VALUES (?, ?, 3, ?, 1, 0)
            """,
            (project_name, str(compose_path), next_sort_order(conn)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_project(row)


def discover_existing_projects() -> None:
    for compose_path in discover_existing_compose_paths():
        project_name = compose_path.parent.name
        project = add_discovered_project(project_name, compose_path)
        if project and main_loop:
            message = f"自动发现新项目 {project_name}，已加入队列末尾"
            main_loop.call_soon_threadsafe(
                lambda project=project, message=message: asyncio.create_task(publish("discovered", message, project))
            )


def docker_event_listener() -> None:
    try:
        client = docker.from_env()
        for event in client.events(decode=True, filters={"type": "container", "event": "start"}):
            attrs = event.get("Actor", {}).get("Attributes", {})
            project_name = attrs.get("com.docker.compose.project")
            if not project_name:
                continue
            compose_path = discover_compose_path(project_name, attrs)
            if compose_path:
                project = add_discovered_project(project_name, compose_path)
                if project and main_loop:
                    message = f"自动发现新项目 {project_name}，已加入队列末尾"
                    main_loop.call_soon_threadsafe(
                        lambda project=project, message=message: asyncio.create_task(publish("discovered", message, project))
                    )
    except Exception as exc:
        print(f"auto-discovery stopped: {exc}", flush=True)


async def boot_after_startup_delay() -> None:
    delay = get_boot_on_start_delay()
    if delay > 0:
        await publish("waiting", f"Boot-Manager 启动后等待 {delay} 秒再执行启动队列")
        await asyncio.sleep(delay)

    try:
        await boot_all_projects()
    except HTTPException as exc:
        await publish("error", f"自动启动失败：{exc.detail}")
    except Exception as exc:
        await publish("error", f"自动启动失败：{exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global main_loop
    require_configured_admin_key()
    main_loop = asyncio.get_running_loop()
    init_db()
    discover_existing_projects()
    threading.Thread(target=docker_event_listener, daemon=True).start()
    asyncio.create_task(boot_after_startup_delay())
    yield


app = FastAPI(title="Boot-Manager", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/static/{asset_path:path}")
async def static_asset(asset_path: str) -> FileResponse:
    path = (STATIC_ROOT / asset_path).resolve()
    if STATIC_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path)


@app.get("/api/auth/check")
async def api_auth_check(_: None = Depends(require_api_key)) -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/projects")
async def api_list_projects(_: None = Depends(require_api_key)) -> list[dict[str, Any]]:
    return list_projects()


@app.post("/api/projects", status_code=201)
async def api_create_project(payload: ProjectCreate, _: None = Depends(require_api_key)) -> dict[str, Any]:
    return insert_project(payload)


@app.put("/api/projects/{project_id}")
async def api_update_project(project_id: int, payload: ProjectUpdate, _: None = Depends(require_api_key)) -> dict[str, Any]:
    return update_project(project_id, payload)


@app.post("/api/projects/reorder")
async def api_reorder_projects(payload: ProjectReorder, _: None = Depends(require_api_key)) -> list[dict[str, Any]]:
    return reorder_projects(payload)


@app.delete("/api/projects/{project_id}", status_code=204)
async def api_delete_project(project_id: int, _: None = Depends(require_api_key)) -> None:
    delete_project(project_id)


@app.post("/api/projects/{project_id}/clear-warning")
async def api_clear_warning(project_id: int, _: None = Depends(require_api_key)) -> dict[str, Any]:
    return update_project(project_id, ProjectUpdate(needs_warning=False))


@app.post("/api/boot")
async def api_boot(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return await boot_all_projects()


@app.get("/api/events")
async def api_events(_: None = Depends(require_event_stream_key)) -> StreamingResponse:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    subscribers.add(queue)

    async def stream():
        try:
            yield "event: connected\ndata: {\"message\":\"SSE connected\"}\n\n"
            while True:
                payload = await queue.get()
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: {payload['event']}\ndata: {data}\n\n"
        finally:
            subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")
