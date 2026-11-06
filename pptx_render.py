"""Bounded rendering with Windows PowerPoint, never the user's COM instance."""

import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

RENDER_LOCK = threading.Lock()


def validate_package(content: bytes):
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            entries = package.infolist()
            if len(entries) > 6000 or sum(item.file_size for item in entries) > 180 * 1024 * 1024:
                raise ValueError("PPTX解压后过大，请按章节拆分。")
            for item in entries:
                name = item.filename.lower()
                if any(value in name for value in ("vbaproject", "activex/", "embeddings/")):
                    raise ValueError("当前作品版不支持带宏、ActiveX或嵌入对象的PPTX，请另存为普通PPTX。")
                if name.endswith(".rels"):
                    root = ElementTree.fromstring(package.read(item))
                    for relation in root:
                        # A clickable URL is not fetched during rendering. Linked images/OLE/media can be.
                        if (relation.get("TargetMode") == "External" and
                                not relation.get("Type", "").endswith("/hyperlink")):
                            raise ValueError("PPTX含外部链接资源；请将图片/媒体嵌入文件后重试。")
    except ValueError:
        raise
    except Exception:
        raise ValueError("PPTX压缩包无效或已损坏。") from None


def _powerpoint_pids():
    import win32api
    import win32process
    pids = set()
    for pid in win32process.EnumProcesses():
        try:
            handle = win32api.OpenProcess(0x410, False, pid)
            try:
                if Path(win32process.GetModuleFileNameEx(handle, 0)).name.lower() == "powerpnt.exe":
                    pids.add(pid)
            finally:
                handle.Close()
        except Exception:
            continue
    return pids


def render_slides(source: Path, output: Path, pages: list[int], timeout=120) -> list[dict]:
    if sys.platform != "win32":
        raise ValueError("PPTX渲染目前需要Windows与Microsoft PowerPoint。")
    with RENDER_LOCK:
        output.mkdir(parents=True, exist_ok=True)
        existing = _powerpoint_pids()
        for filename in ("owned.json", "render-status.json"):
            (output / filename).unlink(missing_ok=True)
        request = output / "render-request.json"
        request.write_text(json.dumps({"source": str(source.resolve()), "output": str(output.resolve()),
                           "pages": pages, "existing": sorted(existing), "started_at": time.time()}), encoding="utf-8")
        started = time.time()
        try:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(request)],
                capture_output=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            # Hold/verify the exact newly-created Office process before terminating it.
            _stop_owned(output / "owned.json", existing, started)
            raise ValueError("PowerPoint渲染超过120秒，任务已停止；请缩小幻灯片范围。") from None
        status_file = output / "render-status.json"
        if not status_file.exists():
            _stop_owned(output / "owned.json", existing, started)
            raise ValueError("PowerPoint渲染进程未完成；请确认已安装并初始化Microsoft PowerPoint。")
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if not status.get("ok") or completed.returncode:
            raise ValueError(status.get("error", "PowerPoint渲染失败。"))
        images = [{"page": page, "image": f"slides/slide-{page:04}.png"} for page in pages]
        if any(not (output.parent / image["image"]).is_file() for image in images):
            raise ValueError("部分幻灯片未生成图片，任务停止，不会静默使用纯文字替代。")
        return images


def _stop_owned(marker, existing, started):
    import win32api
    import win32process
    try:
        identity = json.loads(marker.read_text())
        pid, expected_created = identity["pid"], identity.get("created_at")
        if pid in existing or not isinstance(expected_created, (int, float)):
            return
        handle = win32api.OpenProcess(0x411, False, pid)
        try:
            name = Path(win32process.GetModuleFileNameEx(handle, 0)).name.lower()
            created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
            if name == "powerpnt.exe" and created >= started - 1 and created == expected_created:
                win32api.TerminateProcess(handle, 1)
        finally:
            handle.Close()
    except Exception:
        pass


def _new_powerpoint_process(started_at):
    """Find one fresh PowerPoint process without restricted COM HWND members.

    The worker must first verify that no PowerPoint is already running. Refuse
    ambiguous launches, reused PIDs and any process older than this COM launch.
    """
    import win32api
    import win32process
    candidates = _powerpoint_pids()
    if len(candidates) != 1:
        raise ValueError("无法唯一确认新启动的PowerPoint进程，已停止渲染。请保存并关闭PowerPoint后重试。")
    pid = next(iter(candidates))
    handle = win32api.OpenProcess(0x410, False, pid)
    try:
        name = Path(win32process.GetModuleFileNameEx(handle, 0)).name.lower()
        created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
    finally:
        handle.Close()
    if name != "powerpnt.exe" or created < started_at - .1:
        raise ValueError("PowerPoint进程身份或创建时间不符，已停止渲染，未打开或关闭其中的文件。")
    return pid, created


def _worker(request: Path):
    data = json.loads(request.read_text(encoding="utf-8"))
    output = Path(data["output"])
    import pythoncom
    import win32com
    # Keep pywin32's generated COM cache local to this isolated render, rather
    # than modifying the user's shared Temp/gen_py cache or needing elevation.
    win32com.__gen_path__ = str(output / "com-cache")
    import win32com.client
    app = presentation = None
    owned = False
    stage = "检查PowerPoint是否已运行"
    status = {"ok": False, "error": "PowerPoint渲染失败，请确认文件可打开且PowerPoint已完成首次启动。"}
    pythoncom.CoInitialize()
    try:
        # Application.HWND is restricted on some PowerPoint COM servers and
        # raises DISP_E_MEMBERNOTFOUND even though it appears in their typelib.
        # Start from no Office instance so process snapshots cannot select an
        # unrelated existing session. Never close a pre-existing application.
        if data["existing"] or _powerpoint_pids():
            raise ValueError("检测到PowerPoint正在运行。请先保存并关闭所有PowerPoint窗口，再重试；程序不会关闭已有文件。")
        stage = "启动PowerPoint独立实例"
        launched_at = time.time()
        app = win32com.client.DispatchEx("PowerPoint.Application")
        stage = "核对新PowerPoint进程及创建时间"
        pid, created = _new_powerpoint_process(launched_at)
        stage = "检查新PowerPoint实例中是否已有文件"
        if app.Presentations.Count != 0:
            raise ValueError("PowerPoint实例中已有打开的文件，已停止渲染，不会关闭这些文件。请保存并关闭PowerPoint后重试。")
        owned = True
        (output / "owned.json").write_text(json.dumps({"pid": pid, "created_at": created}), encoding="utf-8")
        stage = "设置宏禁用"
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        stage = "只读打开PPTX"
        presentation = app.Presentations.Open(data["source"], ReadOnly=True, Untitled=False, WithWindow=False)
        ratio = presentation.PageSetup.SlideHeight / presentation.PageSetup.SlideWidth
        width, height = 1600, round(1600 * ratio)
        if not 200 <= height <= 3200:
            raise ValueError("幻灯片比例异常，无法在当前渲染预算内处理。")
        for page in data["pages"]:
            stage = f"导出第{page}页"
            presentation.Slides.Item(page).Export(str(output / f"slide-{page:04}.png"), "PNG", width, height)
        status = {"ok": True}
    except Exception as exc:
        code = getattr(exc, "hresult", None)
        # This worker receives only file/rendering metadata, never model keys.
        # Keep the precise native error local instead of hiding a type bug behind
        # an inaccurate instruction to initialize Office.
        status["diagnostic"] = {"stage": stage, "exception": type(exc).__name__, "message": str(exc)[:600]}
        if code == -2147023584:
            status["error"] = "当前服务进程无法使用交互式Office会话（0x80070520）。请在自己的PowerShell中重新启动本项目，再运行PPTX渲染自检。"
        elif isinstance(exc, ValueError):
            status["error"] = str(exc)
        elif isinstance(exc, TypeError):
            status["error"] = f"程序在{stage}时遇到接口类型不兼容（TypeError）。详细原因已保存在本机任务的slides/render-status.json。"
        else:
            code_text = f"0x{code & 0xffffffff:08X}" if isinstance(code, int) else "无"
            status["error"] = f"PowerPoint在{stage}时失败（{type(exc).__name__}，代码{code_text}）。请确认Office可正常启动。"
    finally:
        if owned:
            if presentation is not None:
                try:
                    presentation.Close()
                except Exception:
                    pass
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
        (output / "render-status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        pythoncom.CoUninitialize()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    _worker(Path(sys.argv[2]))
