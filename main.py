from __future__ import annotations

import json
import mimetypes
import os
import secrets
import warnings
from pathlib import Path
from threading import Lock
from typing import Dict, List
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_MEDIA_ROOT = APP_ROOT / "media"
MEDIA_ROOT = Path(os.getenv("PLAYER_MEDIA_ROOT", DEFAULT_MEDIA_ROOT)).resolve()
DATA_DIR = Path(os.getenv("PLAYER_DATA_DIR", APP_ROOT / "data")).resolve()
PROGRESS_FILE = DATA_DIR / "progress.json"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
SESSION_COOKIE = os.getenv("PLAYER_SESSION_COOKIE", "player_session")


def require_env(name: str, *, min_length: int | None = None) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set.")
    if min_length is not None and len(value) < min_length:
        raise RuntimeError(
            f"Environment variable {name} must be at least {min_length} characters."
        )
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = require_env("PLAYER_SECRET_KEY", min_length=32)
AUTH_PASSWORD = require_env("PLAYER_PASSWORD", min_length=8)
SESSION_MAX_AGE = int(os.getenv("PLAYER_SESSION_MAX_AGE", "604800"))
SECURE_COOKIES = env_bool("PLAYER_SECURE_COOKIES", default=True)

if not SECURE_COOKIES:
    warnings.warn(
        "PLAYER_SECURE_COOKIES is disabled; session cookies will not be marked secure.",
        UserWarning,
    )

app = FastAPI(title="Audio Player")
app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=APP_ROOT / "templates")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie=SESSION_COOKIE,
    same_site="lax",
    https_only=SECURE_COOKIES,
    max_age=SESSION_MAX_AGE,
)


def ensure_storage() -> None:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PROGRESS_FILE.exists():
        PROGRESS_FILE.write_text("{}", encoding="utf-8")


class ProgressStore:
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._lock = Lock()
        ensure_storage()

    def _load(self) -> Dict[str, Dict[str, float]]:
        if not self.storage_path.exists():
            return {}
        try:
            content = self.storage_path.read_text(encoding="utf-8")
            if not content.strip():
                return {}
            return json.loads(content)
        except json.JSONDecodeError:
            return {}

    def _save(self, data: Dict[str, Dict[str, float]]) -> None:
        self.storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, key: str) -> Dict[str, float] | None:
        with self._lock:
            data = self._load()
        return data.get(key)

    def set(self, key: str, position: float, duration: float, played: bool) -> None:
        payload = {"position": position, "duration": duration, "played": played}
        with self._lock:
            data = self._load()
            data[key] = payload
            self._save(data)


progress_store = ProgressStore(PROGRESS_FILE)


class ProgressPayload(BaseModel):
    file: str
    position: float
    duration: float


class DirectoryEntry(BaseModel):
    name: str
    path: str


class AudioEntry(BaseModel):
    name: str
    path: str
    played: bool = False


CSRF_SESSION_KEY = "csrf_token"


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token(request: Request, token: str) -> bool:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not expected or not token:
        return False
    try:
        return secrets.compare_digest(token, expected)
    except TypeError:
        return False


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def login_redirect(request: Request) -> RedirectResponse:
    login_url = request.url_for("login")
    location = request.url.path
    if request.url.query:
        location = f"{location}?{request.url.query}"
    target = quote(location)
    return RedirectResponse(
        f"{login_url}?next={target}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/login", response_class=HTMLResponse, name="login")
async def login_page(request: Request) -> HTMLResponse:
    next_path = request.query_params.get("next", "")
    if is_authenticated(request):
        destination = str(request.url_for("index"))
        if next_path:
            candidate = unquote(next_path)
            if candidate.startswith("/"):
                destination = candidate
        return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    context = {
        "request": request,
        "next_path": next_path,
        "error": None,
        "csrf_token": ensure_csrf_token(request),
    }
    return templates.TemplateResponse("login.html", context)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next_path: str = Form(default=""),
) -> Response:
    if not validate_csrf_token(request, csrf_token):
        context = {
            "request": request,
            "next_path": next_path,
            "error": "Session expired. Please try again.",
            "csrf_token": ensure_csrf_token(request),
        }
        return templates.TemplateResponse(
            "login.html", context, status_code=status.HTTP_400_BAD_REQUEST
        )

    if password == AUTH_PASSWORD:
        request.session["authenticated"] = True
        request.session.pop(CSRF_SESSION_KEY, None)
        ensure_csrf_token(request)
        destination = str(request.url_for("index"))
        if next_path:
            candidate = unquote(next_path)
            if candidate.startswith("/"):
                destination = candidate
        return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)

    context = {
        "request": request,
        "next_path": next_path,
        "error": "Invalid username or password",
        "csrf_token": ensure_csrf_token(request),
    }
    return templates.TemplateResponse(
        "login.html", context, status_code=status.HTTP_401_UNAUTHORIZED
    )


@app.post("/logout", name="logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    if not validate_csrf_token(request, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )
    request.session.clear()
    return RedirectResponse(
        str(request.url_for("login")), status_code=status.HTTP_303_SEE_OTHER
    )


def validate_within_media_root(target: Path) -> Path:
    resolved = target.resolve()
    if MEDIA_ROOT not in resolved.parents and resolved != MEDIA_ROOT:
        raise HTTPException(
            status_code=400, detail="Requested path is outside the media root"
        )
    return resolved


def list_directory(
    relative_path: str,
) -> Dict[str, List[DirectoryEntry] | List[AudioEntry]]:
    directory = validate_within_media_root(MEDIA_ROOT / relative_path)
    if not directory.exists() or not directory.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    directories: List[DirectoryEntry] = []
    audio_files: List[AudioEntry] = []
    for entry in sorted(
        directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
    ):
        if entry.name.startswith("."):
            continue
        rel_path = str(entry.relative_to(MEDIA_ROOT))
        if entry.is_dir():
            directories.append(DirectoryEntry(name=entry.name, path=rel_path))
        elif entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
            progress = progress_store.get(rel_path)
            played = bool(progress.get("played")) if progress else False
            audio_files.append(
                AudioEntry(name=entry.name, path=rel_path, played=played)
            )

    return {"directories": directories, "audio_files": audio_files}


def breadcrumb(relative_path: str) -> List[DirectoryEntry]:
    if not relative_path:
        return []
    parts = Path(relative_path).parts
    crumbs: List[DirectoryEntry] = []
    accumulator = Path()
    for part in parts:
        accumulator /= part
        crumbs.append(DirectoryEntry(name=part, path=str(accumulator)))
    return crumbs


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, path: str = "") -> HTMLResponse:
    if not is_authenticated(request):
        return login_redirect(request)
    directory_data = list_directory(path)
    parent_path = None
    if path:
        parent = Path(path).parent
        parent_path = "" if str(parent) == "." else str(parent)
    context = {
        "request": request,
        "current_path": path,
        "breadcrumbs": breadcrumb(path),
        "directories": directory_data["directories"],
        "audio_files": directory_data["audio_files"],
        "parent_path": parent_path,
        "media_root": MEDIA_ROOT.name or str(MEDIA_ROOT),
        "csrf_token": ensure_csrf_token(request),
    }
    return templates.TemplateResponse("index.html", context)


def get_audio_file(relative_path: str) -> Path:
    file_path = validate_within_media_root(MEDIA_ROOT / relative_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    return file_path


@app.get("/media")
async def media(
    request: Request, path: str = Query(..., description="Relative path to audio")
) -> FileResponse:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    file_path = get_audio_file(path)
    media_type, _ = mimetypes.guess_type(str(file_path))
    headers = {"Accept-Ranges": "bytes"}
    return FileResponse(
        path=file_path, media_type=media_type, filename=file_path.name, headers=headers
    )


@app.get("/api/progress")
async def get_progress(
    request: Request, file: str = Query(..., description="Relative audio path")
) -> JSONResponse:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    _ = get_audio_file(file)
    progress = progress_store.get(file)
    if not progress:
        return JSONResponse({"position": 0.0, "duration": 0.0, "played": False})
    return JSONResponse(
        {
            "position": progress.get("position", 0.0),
            "duration": progress.get("duration", 0.0),
            "played": bool(progress.get("played")),
        }
    )


@app.post("/api/progress")
async def set_progress(request: Request, payload: ProgressPayload) -> JSONResponse:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    _ = get_audio_file(payload.file)
    position = max(payload.position, 0.0)
    duration = max(payload.duration, 0.0)
    existing = progress_store.get(payload.file) or {}
    played = bool(existing.get("played"))
    if duration > 0 and position >= duration * 0.95:
        position = 0.0
        played = True
    progress_store.set(payload.file, position, duration, played)
    return JSONResponse(
        {
            "status": "ok",
            "position": position,
            "duration": duration,
            "played": played,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
