from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import tomllib
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "zap.toml"


def _load_config() -> dict:
    config_path = Path(os.environ.get("ZAP_CONFIG", str(_default_config_path())))
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"配置文件不存在: {config_path}") from e
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"配置文件 TOML 解析失败: {config_path}") from e

    server_cfg = data.get("server", {}) or {}

    raw_share_dir = server_cfg.get("share_directory", "./shared")
    share_dir = Path(str(raw_share_dir))
    if not share_dir.is_absolute():
        share_dir = (config_path.parent / share_dir).resolve()
    else:
        share_dir = share_dir.resolve()

    return {
        "config_path": config_path,
        "host": str(server_cfg.get("ip", "0.0.0.0")),
        "port": int(server_cfg.get("port", 8000)),
        "share_dir": share_dir,
    }


CONFIG = _load_config()
SHARE_ROOT: Path = CONFIG["share_dir"]

app = FastAPI(title="zap-server")


def _safe_resolve(relative_path: str) -> Path:
    rel = Path(relative_path or ".")
    if rel.is_absolute():
        raise HTTPException(status_code=400, detail="path 必须是相对路径")
    target = (SHARE_ROOT / rel).resolve()
    if not target.is_relative_to(SHARE_ROOT):
        raise HTTPException(status_code=400, detail="path 越界")
    return target


@app.get("/")
def root():
    return {
        "ok": True,
        "share_dir": str(SHARE_ROOT),
        "config": str(CONFIG["config_path"]),
    }


@app.get("/api/list")
def list_dir(path: str = Query("", max_length=4096)):
    if not SHARE_ROOT.exists():
        raise HTTPException(
            status_code=500,
            detail=f"共享目录不存在: {SHARE_ROOT}",
        )
    if not SHARE_ROOT.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"共享目录不是文件夹: {SHARE_ROOT}",
        )

    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="路径不是文件夹")

    entries: list[dict] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(SHARE_ROOT).as_posix()),
                "is_dir": child.is_dir(),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )

    current = str(target.relative_to(SHARE_ROOT).as_posix())
    if current == ".":
        current = ""

    parent = ""
    if target != SHARE_ROOT:
        parent = str(target.parent.relative_to(SHARE_ROOT).as_posix())
        if parent == ".":
            parent = ""

    return {"path": current, "parent": parent, "entries": entries}


@app.get("/api/download/file")
def download_file(path: str = Query(..., max_length=4096)):
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="目标不是文件")
    return FileResponse(path=target, filename=target.name)


@app.get("/api/tree")
def tree(path: str = Query("", max_length=4096)):
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="目标不是文件夹")

    directories: list[str] = []
    files: list[dict] = []

    for root, dirnames, filenames in os.walk(target, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(target)

        keep_dirnames: list[str] = []
        for d in dirnames:
            p = root_path / d
            try:
                if p.is_symlink():
                    continue
            except OSError:
                continue
            keep_dirnames.append(d)
            rel = (rel_root / d)
            if rel != Path("."):
                directories.append(rel.as_posix())
        dirnames[:] = keep_dirnames

        for name in filenames:
            file_path = root_path / name
            try:
                if file_path.is_symlink():
                    continue
                stat = file_path.stat()
            except OSError:
                continue
            rel_file = (rel_root / name)
            files.append(
                {
                    "rel_path": rel_file.as_posix(),
                    "share_path": str(file_path.relative_to(SHARE_ROOT).as_posix()),
                    "size": int(stat.st_size),
                    "mtime": int(stat.st_mtime),
                }
            )

    root_share_path = str(target.relative_to(SHARE_ROOT).as_posix())
    if root_share_path == ".":
        root_share_path = ""

    return {
        "root": root_share_path,
        "directories": sorted(set(directories)),
        "files": files,
    }


def _zip_directory(source_dir: Path) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix="zap_", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    with zipfile.ZipFile(
        tmp_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        for root, dirs, files in os.walk(source_dir, followlinks=False):
            root_path = Path(root)
            rel_root = root_path.relative_to(source_dir)

            if not dirs and not files and rel_root != Path("."):
                zf.writestr(rel_root.as_posix().rstrip("/") + "/", b"")

            for filename in files:
                file_path = root_path / filename
                try:
                    if file_path.is_symlink():
                        continue
                except OSError:
                    continue
                arcname = (rel_root / filename).as_posix()
                zf.write(file_path, arcname=arcname)

    return tmp_path


@app.get("/api/download/folder")
def download_folder(
    background_tasks: BackgroundTasks,
    path: str = Query("", max_length=4096),
):
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="目标不是文件夹")

    zip_path = _zip_directory(target)
    background_tasks.add_task(os.unlink, zip_path)

    base_name = target.name or "folder"
    return FileResponse(
        path=zip_path,
        filename=f"{base_name}.zip",
        media_type="application/zip",
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])


if __name__ == "__main__":
    main()
