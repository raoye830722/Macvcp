# -*- coding: utf-8 -*-
"""macOS 顶层窗口枚举、筛选与统一目标解析（Quartz CGWindowList 实现）。

与 windows.py 保持完全一致的函数签名与返回契约：
- 候选字典沿用 "hwnd" 键名，值为 CGWindowID（macOS 窗口标识），
  下游代码与 JSON 契约无需改动；语义注记见 manifest macOS 部分。
- windowRect 等坐标一律为逻辑点（Retina 物理像素换算由 capture 层处理）。
- className 在 macOS 无对应概念，映射为进程属主名（ownerName）参与筛选，
  以兼容 class_name 筛选路径。
- 仅枚举 layer==0 的普通应用窗口（排除菜单栏/Dock/悬浮 overlay），
  对齐 Windows EnumWindows 的顶层窗口语义。
"""

import os

from .errors import ScreenPilotError
from .geometry import Rect


def _pname_by_pid(pid):
    """按 PID 获取进程名，psutil 优先，失败回退 None。"""
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(int(pid)).name()
    except Exception:
        return None


def _window_list(visible_only=True):
    """返回 CGWindowList 原始条目数组。"""
    import Quartz
    option = Quartz.kCGWindowListExcludeDesktopElements
    if visible_only:
        option |= Quartz.kCGWindowListOptionOnScreenOnly
    else:
        option |= Quartz.kCGWindowListOptionAll
    return list(Quartz.CGWindowListCopyWindowInfo(option, Quartz.kCGNullWindowID) or [])


def _bounds_to_rect(bounds):
    b = dict(bounds or {})
    left = int(b.get("X", 0) or 0)
    top = int(b.get("Y", 0) or 0)
    return Rect.from_ltrb(
        left,
        top,
        left + int(b.get("Width", 0) or 0),
        top + int(b.get("Height", 0) or 0),
    )


def _entry_to_info(w):
    """CGWindowList 条目 → 与 windows.py 契约一致的窗口元数据字典。"""
    window_id = int(w.get("kCGWindowNumber", 0) or 0)
    pid = int(w.get("kCGWindowOwnerPID", 0) or 0)
    owner = str(w.get("kCGWindowOwnerName") or "")
    title = str(w.get("kCGWindowName") or "")
    onscreen = bool(w.get("kCGWindowIsOnscreen", False))
    return {
        "hwnd": window_id,  # 键名沿用 Windows 契约，值为 CGWindowID
        "title": title or f"CGWindow:{window_id}",
        "processId": pid,
        "processName": _pname_by_pid(pid) or (owner or None),
        "className": owner,  # macOS 无窗口类概念，映射为属主名
        "visible": onscreen,
        "minimized": not onscreen,
        "windowRect": _bounds_to_rect(w.get("kCGWindowBounds")).to_dict(),
    }


def process_name_by_hwnd(window_id):
    """根据 CGWindowID 获取所属进程名（沿用 windows.py 函数名）。"""
    try:
        for w in _window_list(visible_only=False):
            if int(w.get("kCGWindowNumber", -1) or -1) == int(window_id):
                return _pname_by_pid(int(w.get("kCGWindowOwnerPID", 0) or 0)) \
                    or (str(w.get("kCGWindowOwnerName") or "") or None)
        return None
    except Exception:
        return None


def window_info(window_id):
    """读取单个窗口的稳定元数据（契约同 windows.py.window_info）。"""
    window_id = int(window_id)
    for w in _window_list(visible_only=False):
        if int(w.get("kCGWindowNumber", -1) or -1) == window_id:
            return _entry_to_info(w)
    raise ScreenPilotError(
        "INVALID_HWND",
        f"CGWindow:{window_id} 不是有效窗口。",
        retryable=True,
    )


def enumerate_windows(*, title=None, process_name=None, pid=None,
                      class_name=None, visible_only=True, min_size=1):
    """枚举并筛选顶层窗口，返回按匹配质量和面积排序的候选（契约同 windows.py）。"""
    title_query = str(title or "").casefold()
    process_query = str(process_name or "").casefold()
    class_query = str(class_name or "").casefold()
    target_pid = int(pid) if pid not in (None, "") else None
    candidates = []

    for w in _window_list(visible_only=visible_only):
        try:
            # 仅保留普通应用窗口层（layer==0），排除菜单栏/Dock/overlay
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                continue

            info = _entry_to_info(w)
            actual_title = info["title"]
            actual_class = info["className"] or ""
            actual_pid = info["processId"]
            actual_process = (info["processName"] or "").casefold()

            if target_pid is not None and int(actual_pid) != target_pid:
                continue
            if title_query and title_query not in actual_title.casefold():
                continue
            if class_query and class_query not in actual_class.casefold():
                continue
            if process_query and process_query not in actual_process:
                continue

            rect = Rect.from_mapping(info["windowRect"])
            if rect.width < min_size or rect.height < min_size:
                continue

            score = 0
            reasons = []
            if title_query:
                if actual_title.casefold() == title_query:
                    score += 100
                    reasons.append("exactTitle")
                else:
                    score += 60
                    reasons.append("containsTitle")
            if process_query:
                if actual_process == process_query:
                    score += 100
                    reasons.append("exactProcessName")
                else:
                    score += 60
                    reasons.append("containsProcessName")
            if target_pid is not None:
                score += 100
                reasons.append("exactPid")
            if class_query:
                if actual_class.casefold() == class_query:
                    score += 80
                    reasons.append("exactClassName")
                else:
                    score += 40
                    reasons.append("containsClassName")

            info.update({
                "area": rect.width * rect.height,
                "score": score,
                "matchReasons": reasons,
            })
            candidates.append(info)
        except Exception:
            continue

    candidates.sort(
        key=lambda item: (item["score"], item["area"]),
        reverse=True,
    )
    return candidates


def resolve_window(args, *, required=False):
    """
    按 hwnd → title → processName → pid/className 解析窗口（契约同 windows.py）。

    返回 (selected, candidates)；未提供条件且 required=False 时返回
    (None, [])。
    """
    normalized = {str(key).lower(): value for key, value in dict(args or {}).items()}
    hwnd = normalized.get("hwnd")
    if hwnd not in (None, ""):
        selected = window_info(int(hwnd))
        selected["selectionReason"] = "explicitHwnd"
        return selected, [selected]

    title = (
        normalized.get("windowtitle")
        or normalized.get("window_title")
        or normalized.get("title")
    )
    process_name = (
        normalized.get("processname")
        or normalized.get("process_name")
        or normalized.get("process")
    )
    pid = normalized.get("pid") or normalized.get("processid")
    class_name = normalized.get("classname") or normalized.get("class_name")

    if not any(value not in (None, "") for value in (title, process_name, pid, class_name)):
        if required:
            raise ScreenPilotError(
                "WINDOW_SELECTOR_REQUIRED",
                "必须提供 hwnd、windowTitle、processName、pid 或 className。",
            )
        return None, []

    candidates = enumerate_windows(
        title=title,
        process_name=process_name,
        pid=pid,
        class_name=class_name,
        min_size=1,
    )
    if not candidates:
        raise ScreenPilotError(
            "WINDOW_NOT_FOUND",
            "未找到符合条件的窗口。",
            retryable=True,
            details={
                "windowTitle": title,
                "processName": process_name,
                "pid": pid,
                "className": class_name,
            },
        )

    selected = dict(candidates[0])
    selected["selectionReason"] = "+".join(
        selected.get("matchReasons") or ["largestArea"]
    )
    return selected, candidates