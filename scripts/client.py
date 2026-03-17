from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import tomllib
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "zap.toml"


def _load_config() -> tuple[dict, Path]:
    config_path = Path(os.environ.get("ZAP_CONFIG", str(_default_config_path())))
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}, config_path

    return data, config_path


def _load_default_server_url() -> str:
    data, _ = _load_config()
    server_cfg = data.get("server", {}) or {}
    ip = str(server_cfg.get("ip", "127.0.0.1"))
    port = int(server_cfg.get("port", 8000))
    return f"http://{ip}:{port}"


def _load_default_download_directory() -> Path:
    data, config_path = _load_config()
    client_cfg = data.get("client", {}) or {}
    raw = client_cfg.get("download_directory", "./downloads")
    p = Path(str(raw))
    if not p.is_absolute():
        p = (config_path.parent / p).resolve()
    else:
        p = p.resolve()
    return p


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: int


class ListWorker(QObject):
    finished = Signal(str, str, list)
    failed = Signal(str)

    def __init__(self, base_url: str, path: str) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._path = path

    def run(self) -> None:
        try:
            with httpx.Client(base_url=self._base_url, timeout=15, trust_env=False) as client:
                resp = client.get("/api/list", params={"path": self._path})
                resp.raise_for_status()
                data = resp.json()
            entries = [
                Entry(
                    name=item["name"],
                    path=item["path"],
                    is_dir=bool(item["is_dir"]),
                    size=int(item["size"]),
                    mtime=int(item["mtime"]),
                )
                for item in data.get("entries", [])
            ]
            self.finished.emit(str(data.get("path", "")), str(data.get("parent", "")), entries)
        except Exception as e:
            self.failed.emit(str(e))


class DownloadWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, params: dict, output_path: Path) -> None:
        super().__init__()
        self._url = url
        self._params = params
        self._output_path = output_path

    def run(self) -> None:
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with httpx.Client(timeout=None, trust_env=False) as client:
                with client.stream("GET", self._url, params=self._params) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    received = 0
                    with self._output_path.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            f.write(chunk)
                            received += len(chunk)
                            self.progress.emit(received, total)
            self.finished.emit(str(self._output_path))
        except Exception as e:
            self.failed.emit(str(e))


class FolderSyncWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, base_url: str, folder_path: str, output_dir: Path) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._folder_path = folder_path
        self._output_dir = output_dir

    def run(self) -> None:
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            with httpx.Client(base_url=self._base_url, timeout=None, trust_env=False) as client:
                resp = client.get("/api/tree", params={"path": self._folder_path})
                resp.raise_for_status()
                data = resp.json()

                directories = [str(p) for p in data.get("directories", [])]
                files = list(data.get("files", []))
                total = 0
                for f in files:
                    try:
                        total += int(f.get("size") or 0)
                    except Exception:
                        pass

                downloaded = 0
                self.progress.emit(downloaded, total)

                for d in directories:
                    (self._output_dir / d).mkdir(parents=True, exist_ok=True)

                for f in files:
                    rel_path = str(f.get("rel_path") or "")
                    share_path = str(f.get("share_path") or "")
                    if not rel_path or not share_path:
                        continue
                    out_path = (self._output_dir / rel_path)
                    out_path.parent.mkdir(parents=True, exist_ok=True)

                    with client.stream("GET", "/api/download/file", params={"path": share_path}) as r:
                        r.raise_for_status()
                        with out_path.open("wb") as out:
                            for chunk in r.iter_bytes(chunk_size=1024 * 256):
                                if not chunk:
                                    continue
                                out.write(chunk)
                                downloaded += len(chunk)
                                self.progress.emit(downloaded, total)

            self.finished.emit(str(self._output_dir))
        except Exception as e:
            self.failed.emit(str(e))


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def _normalize_base_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "http://127.0.0.1:8000"
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    netloc = parts.netloc or parts.path
    path = parts.path if parts.netloc else ""
    if not netloc:
        return "http://127.0.0.1:8000"
    host = parts.hostname or ""
    port = parts.port
    if host == "0.0.0.0":
        host = "127.0.0.1"
    if port is None:
        rebuilt_netloc = host
    else:
        rebuilt_netloc = f"{host}:{port}"
    return urlunsplit((scheme, rebuilt_netloc, path.rstrip("/"), "", ""))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("zap client")

        self._base_url = _normalize_base_url(_load_default_server_url())
        self._download_dir = _load_default_download_directory()
        self._current_path = ""
        self._parent_path = ""

        root = QWidget()
        layout = QVBoxLayout(root)

        top_row = QHBoxLayout()
        self.url_input = QLineEdit(self._base_url)
        self.connect_button = QPushButton("连接")
        self.up_button = QPushButton("上一级")
        self.up_button.setEnabled(False)
        top_row.addWidget(QLabel("Server URL:"))
        top_row.addWidget(self.url_input, 1)
        top_row.addWidget(self.connect_button)
        top_row.addWidget(self.up_button)
        layout.addLayout(top_row)

        self.path_label = QLabel("路径: /")
        layout.addWidget(self.path_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "类型", "大小", "修改时间(秒)"])
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        layout.addWidget(self.tree, 1)

        bottom_row = QHBoxLayout()
        self.download_button = QPushButton("下载选中项")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        bottom_row.addWidget(self.download_button)
        bottom_row.addWidget(self.progress, 1)
        layout.addLayout(bottom_row)

        self.setCentralWidget(root)

        self.connect_button.clicked.connect(self.reload_root)
        self.up_button.clicked.connect(self.go_up)
        self.download_button.clicked.connect(self.download_selected)
        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)

        self._list_thread: QThread | None = None
        self._list_worker: ListWorker | None = None
        self._download_thread: QThread | None = None
        self._download_worker: QObject | None = None

    def _set_busy(self, busy: bool) -> None:
        self.connect_button.setEnabled(not busy)
        self.up_button.setEnabled((not busy) and bool(self._current_path))
        self.download_button.setEnabled(not busy)
        self.tree.setEnabled(not busy)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)

    def reload_root(self) -> None:
        self._current_path = ""
        self._parent_path = ""
        self._base_url = _normalize_base_url(self.url_input.text())
        self.url_input.setText(self._base_url)
        self._list(self._current_path)

    def go_up(self) -> None:
        self._list(self._parent_path)

    def _list(self, path: str) -> None:
        if self._list_thread is not None:
            return
        self._set_busy(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("连接中…")

        worker = ListWorker(self._base_url, path)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_list_finished)
        worker.failed.connect(self._on_list_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_list_thread_finished)
        self._list_thread = thread
        self._list_worker = worker
        thread.start()

    def _on_list_thread_finished(self) -> None:
        self._list_thread = None
        self._list_worker = None
        self._set_busy(False)

    def _on_list_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.statusBar().clearMessage()
        self._show_error(message)

    def _on_list_finished(self, current_path: str, parent_path: str, entries: list) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.statusBar().showMessage("已连接", 2000)
        self._current_path = current_path
        self._parent_path = parent_path
        self.path_label.setText(f"路径: /{self._current_path}".rstrip("/"))
        self.up_button.setEnabled(bool(self._current_path))

        self.tree.clear()
        for entry in entries:
            item = QTreeWidgetItem()
            item.setText(0, entry.name)
            item.setText(1, "文件夹" if entry.is_dir else "文件")
            item.setText(2, "" if entry.is_dir else _human_size(entry.size))
            item.setText(3, str(entry.mtime))
            item.setData(0, Qt.UserRole, entry)
            self.tree.addTopLevelItem(item)

        for i in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(i)

    def on_item_double_clicked(self, item: QTreeWidgetItem) -> None:
        entry = item.data(0, Qt.UserRole)
        if isinstance(entry, Entry) and entry.is_dir:
            self._list(entry.path)

    def download_selected(self) -> None:
        selected = self.tree.selectedItems()
        if not selected:
            return
        entry = selected[0].data(0, Qt.UserRole)
        if not isinstance(entry, Entry):
            return
        if self._download_thread is not None:
            return

        if entry.is_dir:
            output_dir = (self._download_dir / entry.name).resolve()
            self._start_folder_sync(entry.path, output_dir)
            return
        else:
            default_path = str((self._download_dir / entry.name).resolve())
            output_file, _ = QFileDialog.getSaveFileName(
                self,
                "保存文件",
                default_path,
                "All Files (*)",
            )
            if not output_file:
                return
            url = f"{self._base_url}/api/download/file"
            params = {"path": entry.path}
            output_path = Path(output_file)

        self._start_download(url, params, output_path)

    def _start_download(self, url: str, params: dict, output_path: Path) -> None:
        self._set_busy(True)
        self.progress.setValue(0)
        self.statusBar().showMessage("下载中…")

        worker = DownloadWorker(url, params, output_path)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_download_progress)
        worker.finished.connect(self._on_download_finished)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_download_thread_finished)
        self._download_thread = thread
        self._download_worker = worker
        thread.start()

    def _start_folder_sync(self, folder_path: str, output_dir: Path) -> None:
        self._set_busy(True)
        self.progress.setValue(0)
        self.statusBar().showMessage("下载文件夹中…")

        worker = FolderSyncWorker(self._base_url, folder_path, output_dir)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_download_progress)
        worker.finished.connect(self._on_download_finished)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_download_thread_finished)
        self._download_thread = thread
        self._download_worker = worker
        thread.start()

    def _on_download_thread_finished(self) -> None:
        self._download_thread = None
        self._download_worker = None
        self._set_busy(False)

    def _on_download_progress(self, received: int, total: int) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        if self.progress.minimum() == 0 and self.progress.maximum() == 0:
            self.progress.setRange(0, 100)
        percent = int(received * 100 / total) if total else 0
        self.progress.setValue(max(0, min(100, percent)))

    def _on_download_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.statusBar().clearMessage()
        self._show_error(message)

    def _on_download_finished(self, output_path: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.statusBar().showMessage("下载完成", 2000)
        QMessageBox.information(self, "完成", f"已保存到: {output_path}")


def main() -> None:
    app = QApplication([])
    w = MainWindow()
    w.resize(900, 600)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
