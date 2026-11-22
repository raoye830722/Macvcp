# -*- coding: utf-8 -*-
"""macOS 主调度辅助函数集（screen_pilot.py 平台分支专用）。

对齐 screen_pilot.py 内 Windows 专属辅助函数的签名与返回契约：
- find_window_by_title / find_window_by_process / capture_window_smart /
  capture_fullscreen / query_windows 传感器数据 / 后台输入三原语。
- capture_window_smart_darwin 返回 (image, info_dict)，info_dict 键与
  screen_pilot._last_capture_info 完全一致，由调用方赋值全局状态。
- 后台输入使用 CGEventPostToPid（半后台：多数常规应用有效，SIP 保护
  进程与部分沙箱应用无效，届时回退前台路径）。
"""

import json
import os


# ============================================================
# 窗口查找（契约对齐 find_window_by_title / find_window_by_process）
# ============================================================

def find_window_by_title(title_keyword):
    """按标题匹配窗口，返回 (window_id, full_title)。"""
    from .windows_darwin import enumerate_windows

    candidates = enumerate_windows(
        title=title_keyword,
        visible_only=True,
        min_size=1,
    )
    if not candidates:
        return None, None
    selected = candidates[0]
    return selected["hwnd"], selected["title"]


def find_window_by_process(process_name):
    """按进程名解析得分最高的窗口，返回 (window_id, title, process_name)。"""
    from .windows_darwin import enumerate_windows

    candidates = enumerate_windows(
        process_name=process_name,
        visible_only=True,
        min_size=51,
    )
    if not candidates:
        return None, None, None
    selected = candidates[0]
    return (
        selected["hwnd"],
        selected["title"],
        selected.get("processName") or process_name,
    )


# ============================================================
# 智能截图（macOS 单一引擎，无 GDI/DXGI 策略学习）
# ============================================================

def capture_window_smart_darwin(window_id):
    """按窗口 ID 捕获内容，返回 (PIL.Image, info_dict)。

    info_dict 键与 screen_pilot._last_capture_info 完全一致。
    """
    from .capture_darwin import capture_window_by_id
    from .windows_darwin import window_info

    info = window_info(window_id)
    image, capture_rect, scale, backend = capture_window_by_id(window_id)
    debug_log(f"智能截图(darwin): CGWindow={window_id}, 进程={info['processName']}, scale={scale:.2f}")
    return image, {
        "method": "quartz",
        "backend": backend,
        "process_name": info["processName"],
        "learned": False,
        "fallback": False,
        "capture_rect": capture_rect,
    }


def capture_fullscreen_darwin():
    """截取整个虚拟桌面，返回 (PIL.Image, info_dict)。"""
    from .capture_darwin import capture_full_virtual_desktop

    image, virtual, backend = capture_full_virtual_desktop(debug_log)
    return image, {
        "method": "fullscreen",
        "backend": backend,
        "process_name": None,
        "learned": False,
        "fallback": False,
        "capture_rect": virtual,
    }


# ============================================================
# QueryWindows 传感器数据（对齐 WindowSensor fold_blocks 输出形态）
# ============================================================

def _node_name():
    """读取 WindowSensor/plugin-config.json 的 NodeName，缺省 macOSNode。"""
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(current_dir, "..", "WindowSensor", "plugin-config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
                name = cfg.get("NodeName")
                if name:
                    return str(name)
    except Exception:
        pass
    return "macOSNode"


def query_windows_sensor_darwin():
    """枚举窗口并构造与 WindowSensor 相同形态的 fold_blocks JSON 数据。

    返回 parsed_data 字典（vcp_dynamic_fold 结构）。
    """
    from .windows_darwin import enumerate_windows

    candidates = enumerate_windows(visible_only=True, min_size=1)
    lines = []
    for c in candidates:
        lines.append(
            f"{c['processId']}|{c.get('processName') or 'Unknown'}|"
            f"{c['hwnd']}|{c['title']}"
        )
    briefing = (
        f"成功获取窗口状态\n"
        f"[{_node_name()}] macOS 节点 · 可见应用窗口 {len(candidates)} 个\n"
        + "\n".join(f"  - {line}" for line in lines[:40])
    )
    return {
        "vcp_dynamic_fold": True,
        "fold_name": "WindowSense_macOS",
        "plugin_description": "macOS 窗口感知（ScreenPilot 内置 Quartz 实现）",
        "fold_blocks": [
            {"threshold": 0.7, "content": briefing},
            {"threshold": 0.4, "content": f"[{_node_name()}] macOS 节点 · {len(candidates)} 个可见窗口"},
            {"threshold": 0.0, "content": f"[{_node_name()}] macOS 窗口感知在线"},
        ],
        "windows": [
            {
                "pid": c["processId"],
                "processName": c.get("processName"),
                "windowId": c["hwnd"],
                "title": c["title"],
                "rect": c["windowRect"],
            }
            for c in candidates
        ],
    }


# ============================================================
# 后台输入三原语（CGEventPostToPid，半后台）
# ============================================================

def _cg_event_module():
    import Quartz
    return Quartz


def background_click(pid, x, y, *, button="left", clicks=1, debug_log=None):
    """向目标进程投递鼠标事件（逻辑坐标，不抢全局焦点）。"""
    q = _cg_event_module()
    button_map = {
        "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGMouseButtonLeft),
        "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGMouseButtonRight),
        "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGMouseButtonCenter),
    }
    down_type, up_type, cg_button = button_map.get(
        str(button or "left").lower(), button_map["left"])
    point = q.CGPointMake(float(x), float(y))

    move = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point, cg_button)
    q.CGEventPostToPid(int(pid), move)

    for i in range(max(1, int(clicks))):
        down = q.CGEventCreateMouseEvent(None, down_type, point, cg_button)
        up = q.CGEventCreateMouseEvent(None, up_type, point, cg_button)
        # 双击：第二次事件设置 clickState=2
        state = i + 1
        q.CGEventSetIntegerValueField(down, q.kCGMouseEventClickState, state)
        q.CGEventSetIntegerValueField(up, q.kCGMouseEventClickState, state)
        q.CGEventPostToPid(int(pid), down)
        q.CGEventPostToPid(int(pid), up)
    if debug_log:
        debug_log(f"后台点击(darwin): pid={pid} ({x},{y}) {button}×{clicks}")
    return True


def background_type_text(pid, text, *, debug_log=None):
    """向目标进程投递键盘输入（逐字符 Unicode 事件，半后台）。"""
    q = _cg_event_module()
    text = str(text or "")
    for ch in text:
        down = q.CGEventCreateKeyboardEvent(None, 0, True)
        q.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
        up = q.CGEventCreateKeyboardEvent(None, 0, False)
        q.CGEventKeyboardSetUnicodeString(up, len(ch), ch)
        q.CGEventPostToPid(int(pid), down)
        q.CGEventPostToPid(int(pid), up)
    if debug_log:
        debug_log(f"后台输入(darwin): pid={pid} {len(text)} 字符")
    return True


def background_scroll(pid, direction, amount, *, x=None, y=None, debug_log=None):
    """向目标进程投递滚轮事件（语义对齐 Windows：down=内容下滚）。"""
    q = _cg_event_module()
    lines = int(amount or 3)
    delta = lines if str(direction or "down").lower() == "up" else -lines
    if x is not None and y is not None:
        point = q.CGPointMake(float(x), float(y))
        move = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point, q.kCGMouseButtonLeft)
        q.CGEventPostToPid(int(pid), move)
    event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, delta)
    q.CGEventPostToPid(int(pid), event)
    if debug_log:
        debug_log(f"后台滚动(darwin): pid={pid} {direction}×{lines} (delta={delta})")
    return True