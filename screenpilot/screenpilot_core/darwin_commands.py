# -*- coding: utf-8 -*-
"""macOS 命令实现层（screen_pilot.py 平台分流专用）。

设计：门卫模式 —— screen_pilot.py 各 cmd_* 入口仅加 2 行 darwin 分流，
全部 macOS 逻辑收敛于本文件；Windows 路径一行不动，上游同步零冲突。

契约对齐：
- 返回结构与 screen_pilot.py 同名 cmd_* 完全一致（status/result/error）。
- hwnd 键名沿用 Windows 契约，值为 CGWindowID。
- 前台输入复用 pyautogui（跨平台）；后台输入走 host_darwin CGEvent。
- Retina：capture_rect 为逻辑坐标，图像为物理像素，OCR 像素→屏幕点
  换算时除以 scale（由 _resolve_capture_target_darwin 统一注入）。
"""

import base64
import io
import os
import re
import time
from datetime import datetime

from .errors import ScreenPilotError
from .geometry import Rect, is_point_on_virtual_screen
from . import host_darwin


debug_log = None


def _ensure_debug():
    global debug_log
    if debug_log is None:
        def _log(msg):
            try:
                print(f"[ScreenPilot darwin] {msg}", flush=True)
            except Exception:
                pass
        debug_log = _log
    return debug_log


def normalize_args(args):
    """与 screen_pilot.normalize_args 同语义：键名小写化。"""
    return {str(k).lower(): v for k, v in dict(args or {}).items()}


def _resolve_hwnd_darwin(a):
    """统一窗口解析，返回 (window_id, title) 或 (None, error_message)。"""
    from .windows_darwin import resolve_window

    try:
        selected, _ = resolve_window(a, required=False)
    except ScreenPilotError as error:
        return None, error.message
    except Exception as error:
        return None, f"窗口解析失败: {error}"
    if selected is None:
        return None, None
    return int(selected["hwnd"]), selected["title"]


def image_to_base64(img, fmt="PNG", quality=85):
    """与 screen_pilot.image_to_base64 同实现。"""
    normalized_fmt = str(fmt or "PNG").strip().upper()
    if normalized_fmt in ("JPG", "JPEG"):
        normalized_fmt = "JPEG"
        if img.mode != "RGB":
            img = img.convert("RGB")
        save_options = {
            "format": "JPEG",
            "quality": max(1, min(100, int(quality))),
            "optimize": False,
            "progressive": False,
        }
        mime = "image/jpeg"
    else:
        normalized_fmt = "PNG"
        save_options = {"format": "PNG"}
        mime = "image/png"
    buf = io.BytesIO()
    img.save(buf, **save_options)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _get_screenshot_dir():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.environ.get("SCREENPILOT_SCREENSHOT_DIR") or os.path.join(base, "screenshots")
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================
# 截图目标解析（对齐 _resolve_capture_target / cmd_screen_capture）
# ============================================================

def _resolve_capture_target_darwin(a):
    """
    按 hwnd → windowTitle → processName → 全屏 解析截图目标。
    返回 (img, window_id, capture_rect, captured_title, scale)。
    """
    from .capture_darwin import capture_window_by_id, capture_full_virtual_desktop
    from .windows_darwin import window_info

    hwnd = a.get("hwnd")
    window_title = a.get("windowtitle") or a.get("window_title") or a.get("title")
    process_name = a.get("processname") or a.get("process_name") or a.get("process")

    if hwnd:
        wid = int(hwnd)
        info = window_info(wid)
        img, capture_rect, scale, backend = capture_window_by_id(wid, debug_log=_ensure_debug())
        return img, wid, capture_rect, info["title"], scale

    if window_title:
        wid, title = _resolve_hwnd_darwin({"windowTitle": window_title})
        if wid is None:
            return None, None, None, f"未找到标题包含 '{window_title}' 的窗口。", None
        info = window_info(wid)
        img, capture_rect, scale, backend = capture_window_by_id(wid, debug_log=_ensure_debug())
        return img, wid, capture_rect, title, scale

    if process_name:
        wid, title, _pname = host_darwin.find_window_by_process(process_name)
        if wid is None:
            return None, None, None, f"未找到进程名包含 '{process_name}' 的窗口。", None
        info = window_info(wid)
        img, capture_rect, scale, backend = capture_window_by_id(wid, debug_log=_ensure_debug())
        return img, wid, capture_rect, title, scale

    img, virtual, backend = capture_full_virtual_desktop(debug_log=_ensure_debug())
    vrect = Rect.from_mapping(virtual)
    scale = img.width / max(1, vrect.width)
    return img, None, virtual, "全屏截图", scale


def run_ocr_darwin(img, capture_rect, scale, min_confidence=0.0):
    """OCR 并返回屏幕逻辑点坐标。

    坐标语义（darwin）：capture_rect 传逻辑坐标 rect，图像为物理像素。
    run_ocr 内部会把图像像素除以 scale（Retina 2x）——物理像素点 ÷ scale
    恰好等于逻辑点，再叠加逻辑 capture_rect 偏移后，clickablePoint 的
    数值即屏幕逻辑点（space 字段名义为 screenPhysical，darwin 下按
    逻辑点消费，与 ClickAt/pyautogui 的逻辑坐标系一致）。
    """
    from .ocr import run_ocr as run_ocr_engine

    return run_ocr_engine(img, capture_rect, debug_log=_ensure_debug(),
                          min_confidence=min_confidence)


# ============================================================
# ScreenCapture
# ============================================================

def cmd_screen_capture_darwin(args):
    a = normalize_args(args)
    save = str(a.get("save", "false")).lower() in ("true", "1", "yes")
    do_ocr = str(a.get("ocr", "false")).lower() in ("true", "1", "yes")
    filename = a.get("filename")
    try:
        min_ocr_confidence = float(a.get("minconfidence") or a.get("min_confidence") or 0.0)
    except (TypeError, ValueError):
        min_ocr_confidence = 0.0
    output_format = str(
        a.get("format") or a.get("imageformat") or a.get("image_format") or "jpeg"
    ).strip().lower()
    if output_format not in ("png", "jpg", "jpeg"):
        output_format = "jpeg"
    try:
        jpeg_quality = max(1, min(100, int(a.get("quality") or a.get("jpegquality") or 85)))
    except (TypeError, ValueError):
        jpeg_quality = 85

    img, wid, capture_rect, captured_title, scale = _resolve_capture_target_darwin(a)
    if img is None:
        return {"status": "error", "error": captured_title}

    width, height = img.size
    encoded_format = "PNG" if output_format == "png" else "JPEG"
    data_uri = image_to_base64(img, fmt=encoded_format, quality=jpeg_quality)

    text_parts = [
        f"截图成功: {captured_title}",
        f"分辨率: {width} × {height} 像素",
        f"传输格式: {encoded_format}" + (f"（质量 {jpeg_quality}）" if encoded_format == "JPEG" else ""),
        f"截图方式: Quartz ({'窗口内容捕获' if wid else '屏幕区域捕获'}，Retina scale={scale:.2f})",
    ]
    if wid:
        text_parts.append(
            f"窗口屏幕位置: 左上角({capture_rect['x']}, {capture_rect['y']})  "
            f"尺寸 {capture_rect['width']}×{capture_rect['height']}（逻辑点）"
        )
        text_parts.append(
            "提示: 截图为物理像素（Retina），OCR/视觉坐标已换算为屏幕逻辑点；"
            "ClickAt 使用逻辑点坐标。"
        )

    saved_path = None
    if save:
        screenshot_dir = _get_screenshot_dir()
        if not filename:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_title = re.sub(r'[<>:"/\\|?*]', '_', (captured_title or "screenshot")[:30])
            filename = f"{safe_title}_{ts}.png"
        save_path = os.path.join(screenshot_dir, filename)
        img.save(save_path, "PNG")
        saved_path = save_path
        text_parts.append(f"已保存到: {save_path}")

    ocr_blocks = None
    if do_ocr:
        try:
            ocr_blocks = run_ocr_darwin(img, capture_rect, scale, min_confidence=min_ocr_confidence)
            text_parts.append(f"\nOCR 检测到 {len(ocr_blocks)} 个文本区域:")
            for i, blk in enumerate(ocr_blocks, 1):
                cp = blk["clickablePoint"]
                text_parts.append(
                    f"  [{i}] \"{blk['text']}\" → 点击({cp['x']},{cp['y']}) "
                    f"置信度:{blk['confidence']}"
                )
        except Exception as e:
            _ensure_debug()(f"OCR 失败: {e}")
            text_parts.append(f"\nOCR 检测失败: {e}")

    result = {
        "content": [
            {"type": "text", "text": "\n".join(text_parts)},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
        "resolution": {"width": width, "height": height},
    }
    if wid:
        result["windowRect"] = capture_rect
    result["captureRect"] = capture_rect
    if saved_path:
        result["savedPath"] = saved_path
    if ocr_blocks is not None:
        result["ocrResults"] = ocr_blocks
    result["captureMethod"] = "quartz"
    result["captureBackend"] = "quartz_window_capture" if wid else "quartz_region"
    return {"status": "success", "result": result}


# ============================================================
# QueryWindows（内置 Quartz 传感器，不依赖 sensor.ps1）
# ============================================================

def cmd_query_windows_darwin(args):
    try:
        parsed = host_darwin.query_windows_sensor_darwin()
    except Exception as e:
        return {"status": "error", "error": f"macOS 窗口枚举异常: {e}"}

    detailed_text = parsed["fold_blocks"][0]["content"]
    return {
        "status": "success",
        "result": {
            "content": [{"type": "text", "text": detailed_text}],
            "sensorData": parsed,
        },
    }


# ============================================================
# ClickAt（双轨：前台 pyautogui / 后台 CGEventPostToPid）
# ============================================================

def cmd_click_at_darwin(args):
    import pyautogui
    a = normalize_args(args)

    x = a.get("x")
    y = a.get("y")
    if x is None or y is None:
        return {"status": "error", "error": "必须提供 x 和 y 坐标参数。"}
    x = int(x)
    y = int(y)
    button = str(a.get("button", "left")).lower()
    clicks = int(a.get("clicks", 1))
    background = str(a.get("background") or a.get("bg") or "false").lower() in ("true", "1", "yes")
    relative_to_window = str(
        a.get("relativetowindow") or a.get("relative_to_window") or a.get("relative") or "false"
    ).lower() in ("true", "1", "yes")

    if button not in ("left", "right", "middle"):
        return {"status": "error", "error": f"无效的button参数: '{button}'，可选值: left, right, middle。"}

    if background:
        wid, title = _resolve_hwnd_darwin(a)
        if wid is None:
            return {"status": "error", "error": f"后台模式（background）必须提供 hwnd 或 windowTitle 来指定目标窗口。{title or ''}"}

        from .windows_darwin import window_info
        info = window_info(wid)
        pid = info["processId"]

        if relative_to_window:
            rect = Rect.from_mapping(info["windowRect"])
            screen_x = rect.left + x
            screen_y = rect.top + y
            source_desc = f"窗口相对({x},{y})"
        else:
            screen_x, screen_y = x, y
            source_desc = f"屏幕绝对({x},{y})"

        host_darwin.background_click(pid, screen_x, screen_y, button=button, clicks=clicks, debug_log=_ensure_debug())
        return {
            "status": "success",
            "result": (
                f"[后台模式·macOS] 已向窗口 \"{title}\" (CGWindow:{wid}, pid:{pid}) 发送 "
                f"{button} 键点击 {clicks} 次。\n坐标: {source_desc} → 屏幕({screen_x},{screen_y})（逻辑点）\n"
                f"鼠标光标未移动。注: macOS 后台点击为 CGEvent 半后台语义，"
                f"SIP 保护进程与部分沙箱应用可能无效，届时请改前台模式。"
            ),
        }

    # 前台模式
    coord_mode = "屏幕绝对"
    wid = a.get("hwnd")
    if wid:
        wid = int(wid)
        try:
            from .windows_darwin import window_info
            info = window_info(wid)
            rect = Rect.from_mapping(info["windowRect"])
            if relative_to_window:
                orig_x, orig_y = x, y
                x = rect.left + x
                y = rect.top + y
                coord_mode = f"窗口相对({orig_x},{orig_y}) → 屏幕绝对"
        except Exception as e:
            _ensure_debug()(f"窗口信息获取失败: {e}")
            if relative_to_window:
                return {"status": "error", "error": f"无法获取窗口位置进行坐标转换: {e}"}
    elif relative_to_window:
        return {"status": "error", "error": "使用 relativeToWindow 时必须同时提供 hwnd 参数。"}

    from .geometry import get_virtual_screen_rect_darwin
    virtual = get_virtual_screen_rect_darwin()
    if not is_point_on_virtual_screen(x, y, virtual):
        return {
            "status": "error",
            "error": (
                f"最终屏幕坐标 ({x}, {y}) 超出虚拟桌面范围 "
                f"({virtual['x']},{virtual['y']} {virtual['width']}×{virtual['height']})。"
            ),
        }

    pyautogui.click(x, y, button=button, clicks=clicks)
    result_text = f"已在{coord_mode}坐标 ({x}, {y}) 执行 {button} 键点击 {clicks} 次。"
    if wid:
        result_text += f"\n(窗口 CGWindow:{wid} 已可交互)"
    return {"status": "success", "result": result_text}


# ============================================================
# TypeText（双轨：前台 pyautogui / 后台 CGEvent 键盘）
# ============================================================

def _paste_text_darwin(text):
    """非 ASCII 文本前台输入：写 NSPasteboard + Cmd+V 粘贴。"""
    import pyautogui
    try:
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        old = None
        try:
            old = pb.stringForType_(AppKit.NSPasteboardTypeString)
        except Exception:
            pass
        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        pyautogui.hotkey("command", "v")
        time.sleep(0.1)
        if old is not None:
            time.sleep(0.05)
            pb.clearContents()
            pb.setString_forType_(old, AppKit.NSPasteboardTypeString)
        return True
    except Exception as e:
        _ensure_debug()(f"剪贴板粘贴失败，回退 typewrite: {e}")
        pyautogui.typewrite(text, interval=0.01)
        return True


def cmd_type_text_darwin(args):
    import pyautogui
    a = normalize_args(args)

    text = a.get("text") or a.get("content") or a.get("value")
    if not text:
        return {"status": "error", "error": "必须提供 text 参数指定要输入的文本内容。"}
    text = str(text)
    press_enter = str(a.get("enter") or a.get("pressenter") or a.get("submit") or "false").lower() in ("true", "1", "yes")

    wid, title = _resolve_hwnd_darwin(a)

    if wid:
        from .windows_darwin import window_info
        info = window_info(wid)
        pid = info["processId"]
        host_darwin.background_type_text(pid, text, debug_log=_ensure_debug())
        if press_enter:
            import Quartz
            down = Quartz.CGEventCreateKeyboardEvent(None, 36, True)
            up = Quartz.CGEventCreateKeyboardEvent(None, 36, False)
            Quartz.CGEventPostToPid(int(pid), down)
            Quartz.CGEventPostToPid(int(pid), up)
        return {
            "status": "success",
            "result": (
                f"[后台模式·macOS] 已向窗口 \"{title}\" (CGWindow:{wid}, pid:{pid}) 发送文本输入。\n"
                f"输入内容: \"{text[:100]}{'...' if len(text) > 100 else ''}\"\n"
                f"字符数: {len(text)}"
                + ("\n已按下回车键" if press_enter else "")
                + "\n键盘焦点未被劫持（CGEvent 半后台语义）。"
            ),
        }

    # 前台模式：ASCII 直接键入；非 ASCII 走剪贴板粘贴（macOS 原生 Unicode 支持）
    if text.isascii():
        pyautogui.typewrite(text, interval=0.01)
    else:
        _paste_text_darwin(text)

    if press_enter:
        pyautogui.press("enter")

    return {
        "status": "success",
        "result": (
            f"已输入文本。\n"
            f"输入内容: \"{text[:100]}{'...' if len(text) > 100 else ''}\"\n"
            f"字符数: {len(text)}"
            + ("\n已按下回车键" if press_enter else "")
        ),
    }


# ============================================================
# ScrollAt（双轨：前台 pyautogui / 后台 CGEvent 滚轮）
# ============================================================

def cmd_scroll_at_darwin(args):
    import pyautogui
    a = normalize_args(args)

    direction = str(a.get("direction") or a.get("dir") or "").lower()
    if direction not in ("up", "down"):
        return {"status": "error", "error": "必须提供 direction 参数，可选值: up, down。"}
    try:
        amount = int(a.get("amount") or 3)
    except (TypeError, ValueError):
        amount = 3
    x = a.get("x")
    y = a.get("y")
    background = str(a.get("background") or a.get("bg") or "false").lower() in ("true", "1", "yes")

    if background:
        wid, title = _resolve_hwnd_darwin(a)
        if wid is None:
            return {"status": "error", "error": f"后台模式必须提供窗口选择器。{title or ''}"}
        from .windows_darwin import window_info
        info = window_info(wid)
        host_darwin.background_scroll(
            info["processId"], direction, amount, x=x, y=y, debug_log=_ensure_debug())
        return {
            "status": "success",
            "result": f"[后台模式·macOS] 已向 \"{title}\" (pid:{info['processId']}) 发送 {direction} 滚动 {amount} 格。",
        }

    if x is not None and y is not None:
        pyautogui.moveTo(int(x), int(y))
    pyautogui.scroll(amount if direction == "up" else -amount)
    return {
        "status": "success",
        "result": f"已在屏幕坐标 ({x},{y}) 执行 {direction} 滚动 {amount} 格。",
    }


# ============================================================
# ClickText（OCR 匹配点击，darwin 版）
# ============================================================

def _strip_noise(s):
    """去除空格、标点和特殊符号，只留字母数字和 CJK（对齐主调度同名逻辑）。"""
    return re.sub(
        r'[\s\u3000!-/:-@\[-`{-~\u2000-\u206f\u3000-\u303f\uff00-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65\u2010-\u2027\u2030-\u205e\u00a0-\u00bf]',
        '', s)


def cmd_click_text_darwin(args):
    import pyautogui
    a = normalize_args(args)

    target_text = a.get("text") or a.get("target") or a.get("label")
    if not target_text:
        return {"status": "error", "error": "必须提供 text 参数指定要点击的文本内容。"}

    button = str(a.get("button", "left")).lower()
    clicks = int(a.get("clicks", 1))
    match_mode = str(a.get("matchmode") or a.get("match_mode") or a.get("match") or "fuzzy").lower()
    index = int(a.get("index") or a.get("nth") or 1)

    # 1. 截图（darwin 链路：Quartz + Retina scale）
    img, hwnd, capture_rect, captured_title, scale = _resolve_capture_target_darwin(a)
    if img is None:
        return {"status": "error", "error": captured_title}

    # 2. OCR（坐标已换算为屏幕逻辑点）
    try:
        ocr_blocks = run_ocr_darwin(img, capture_rect, scale)
    except Exception as e:
        return {"status": "error", "error": f"OCR 检测失败: {e}"}

    if not ocr_blocks:
        return {"status": "error", "error": "截图中未检测到任何文本。"}

    # 3. 匹配（对齐主调度四种 matchMode）
    matches = []
    target_lower = target_text.lower()
    target_stripped = _strip_noise(target_lower)
    for blk in ocr_blocks:
        blk_lower = str(blk["text"]).lower()
        blk_stripped = _strip_noise(blk_lower)
        if match_mode == "exact":
            if blk_lower == target_lower:
                matches.append(blk)
        elif match_mode == "startswith":
            if blk_lower.startswith(target_lower):
                matches.append(blk)
        elif match_mode == "contains":
            if target_lower in blk_lower:
                matches.append(blk)
        else:  # fuzzy 默认
            if target_stripped and target_stripped in blk_stripped:
                matches.append(blk)

    if not matches:
        all_texts = [f'"{b["text"]}"' for b in ocr_blocks[:20]]
        return {
            "status": "error",
            "error": f"未找到包含 '{target_text}' 的文本。\n检测到的文本: {', '.join(all_texts)}",
        }

    if index > len(matches):
        return {
            "status": "error",
            "error": f"找到 {len(matches)} 个匹配 '{target_text}' 的文本，但请求的是第 {index} 个。",
        }
    selected = matches[index - 1]
    click_x = selected["clickablePoint"]["x"]
    click_y = selected["clickablePoint"]["y"]

    # 4. 点击（前台 pyautogui；后台模式走 CGEvent）
    background = str(a.get("background") or a.get("bg") or "false").lower() in ("true", "1", "yes")
    if background and hwnd:
        from .windows_darwin import window_info
        info = window_info(hwnd)
        host_darwin.background_click(
            info["processId"], click_x, click_y,
            button=button, clicks=clicks, debug_log=_ensure_debug())
        mode_desc = f"[后台模式·macOS] pid:{info['processId']}"
    else:
        pyautogui.click(click_x, click_y, button=button, clicks=clicks)
        mode_desc = "[前台模式]"

    return {
        "status": "success",
        "result": (
            f"{mode_desc} 已点击文本 \"{selected['text']}\"。\n"
            f"点击坐标: ({click_x}, {click_y})（屏幕逻辑点）\n"
            f"匹配模式: {match_mode}，第 {index}/{len(matches)} 个匹配。"
        ),
    }


# ============================================================
# ClickVisual（涂色差分视觉点击，darwin 版）
# ============================================================

def cmd_click_visual_darwin(args):
    """涂色差分点击 darwin 版。

    平台无关部分（vision API / find_white_marker / 调试存图）经函数级
    延迟导入复用 screen_pilot 主调度实现（调用时已加载完毕，无循环导入）。
    darwin 差异：截图为物理像素，差分坐标须除以 scale 再加 capture_rect 偏移；
    置前窗口用 osascript frontmost（替代 win32gui.SetForegroundWindow）。
    """
    import pyautogui
    from PIL import Image
    a = normalize_args(args)

    # 延迟导入主调度的平台无关工具
    import importlib
    sp = importlib.import_module("screen_pilot")
    call_vision_edit_api = sp.call_vision_edit_api
    find_white_marker = None
    from .image_edit import find_white_marker as _fwm
    find_white_marker = _fwm

    description = a.get("description") or a.get("target") or a.get("desc") or a.get("text")
    if not description:
        return {"status": "error", "error": "必须提供 description 参数描述要点击的视觉元素。"}

    button = str(a.get("button", "left")).lower()
    clicks = int(a.get("clicks", 1))

    # 1. 截图（darwin 链路：img 为物理像素，capture_rect 为逻辑坐标）
    img, hwnd, capture_rect, captured_title, scale = _resolve_capture_target_darwin(a)
    if img is None:
        return {"status": "error", "error": captured_title}

    # 2. 原图转 base64
    original_data_uri = image_to_base64(img, fmt="PNG")
    _ensure_debug()(f"ClickVisual: 截图完成 {img.size}, 准备调用图像编辑模型...")

    # 3. 涂色指令（与主调度一致）
    edit_prompt = (
        f"请在图中找到「{description}」的精确位置，"
        f"然后在它的正中心画一个纯白色(#FFFFFF)实心圆，"
        f"圆的大小刚好能完全覆盖住或者标记目标正中心即可，不要画太大。"
        f"圆心必须对准目标的中心。其他区域保持不变。"
    )

    # 4. 调用图像编辑 API
    try:
        edited_bytes = call_vision_edit_api(original_data_uri, edit_prompt)
    except Exception as e:
        return {"status": "error", "error": f"图像编辑 API 调用失败: {e}"}

    # 5. 解析编辑后的图片
    try:
        edited_img = Image.open(io.BytesIO(edited_bytes)).convert("RGB")
    except Exception as e:
        return {"status": "error", "error": f"无法解析 API 返回的图片: {e}"}

    # 5.5 调试图片
    debug_dir = os.path.join(_get_screenshot_dir(), "debug")
    os.makedirs(debug_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    orig_path = os.path.join(debug_dir, f"clickvisual_{ts}_original.png")
    edit_path = os.path.join(debug_dir, f"clickvisual_{ts}_edited.png")
    img.save(orig_path, "PNG")
    edited_img_resized = edited_img.resize(img.size, resample=3) if edited_img.size != img.size else edited_img
    edited_img_resized.save(edit_path, "PNG")

    # 6. 白色标记差分定位（物理像素坐标）
    paint_result = find_white_marker(img, edited_img)
    if paint_result is None:
        return {
            "status": "error",
            "error": f"在编辑后的图中未检测到白色圆球，模型可能没有正确标记 '{description}'。\n调试图片已保存: {debug_dir}",
        }

    marker_x, marker_y, bbox, painted_pct = paint_result
    if painted_pct > 30:
        return {
            "status": "error",
            "error": f"白色区域占比 {painted_pct}% 过大，模型可能画错了。\n调试图片已保存: {debug_dir}",
        }

    # 7. 物理像素 → 屏幕逻辑点：先除 scale，再加 capture_rect 偏移
    if scale and abs(scale - 1.0) > 0.01:
        marker_x = marker_x / scale
        marker_y = marker_y / scale
        bbox = {
            "x": int(bbox["x"] / scale), "y": int(bbox["y"] / scale),
            "width": int(bbox["width"] / scale), "height": int(bbox["height"] / scale),
        }
    if capture_rect:
        click_x = capture_rect["x"] + int(marker_x)
        click_y = capture_rect["y"] + int(marker_y)
    else:
        click_x, click_y = int(marker_x), int(marker_y)

    # 8. 置前窗口（osascript，替代 SetForegroundWindow）
    if hwnd:
        try:
            from .windows_darwin import window_info
            info = window_info(hwnd)
            pid = info["processId"]
            pname = info.get("processName") or ""
            import subprocess
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {int(pid)}) to true'],
                capture_output=True, timeout=3,
            )
            time.sleep(0.3)
        except Exception as e:
            _ensure_debug()(f"置前窗口失败（继续尝试点击）: {e}")

    from .geometry import get_virtual_screen_rect_darwin, is_point_on_virtual_screen
    virtual = get_virtual_screen_rect_darwin()
    if not is_point_on_virtual_screen(click_x, click_y, virtual):
        return {
            "status": "error",
            "error": (
                f"计算出的坐标 ({click_x}, {click_y}) 超出虚拟桌面范围 "
                f"({virtual['x']},{virtual['y']} {virtual['width']}×{virtual['height']})。"
            ),
        }

    pyautogui.click(click_x, click_y, button=button, clicks=clicks)

    result_text = (
        f"已通过视觉定位点击: \"{description}\"\n"
        f"屏幕坐标: ({click_x}, {click_y})（逻辑点，Retina scale={scale:.2f}）\n"
        f"涂色区域: ({bbox['x']},{bbox['y']}) {bbox['width']}×{bbox['height']} "
        f"占比 {painted_pct}%\n"
        f"来源: {captured_title}"
    )
    return {
        "status": "success",
        "result": {
            "content": [{"type": "text", "text": result_text}],
            "clickedPoint": {"x": click_x, "y": click_y},
            "paintedRegion": bbox,
            "paintedPercentage": painted_pct,
        },
    }

# ============================================================
# WaitForText（OCR 轮询，darwin 版）
# ============================================================

def cmd_wait_for_text_darwin(args):
    a = normalize_args(args)
    text = a.get("text")
    if not text:
        return {"status": "error", "error": "WaitForText 必须提供 text 参数。"}
    condition = str(a.get("condition") or "appear").lower()
    try:
        timeout_ms = int(a.get("timeoutms") or a.get("timeout_ms") or 10000)
    except (TypeError, ValueError):
        timeout_ms = 10000
    try:
        poll_ms = int(a.get("pollintervalms") or a.get("poll_interval_ms") or 200)
    except (TypeError, ValueError):
        poll_ms = 200

    deadline = time.time() + timeout_ms / 1000.0
    while True:
        img, _wid, capture_rect, _title, scale = _resolve_capture_target_darwin(a)
        try:
            blocks = run_ocr_darwin(img, capture_rect, scale)
            found = any(str(text).casefold() in str(b["text"]).casefold() for b in blocks)
        except Exception:
            found = False
        if condition == "disappear":
            if not found:
                return {"status": "success", "result": f"文本 \"{text}\" 已消失。"}
        else:
            if found:
                return {"status": "success", "result": f"文本 \"{text}\" 已出现。"}
        if time.time() >= deadline:
            state = "仍存在" if condition == "disappear" else "未出现"
            return {"status": "error", "error": f"等待超时（{timeout_ms}ms）：文本 \"{text}\" {state}。"}
        time.sleep(poll_ms / 1000.0)


# ============================================================
# WaitForElement（AX 轮询，darwin 版；T2 完成后接 uia_darwin）
# ============================================================

def cmd_wait_for_element_darwin(args):
    a = normalize_args(args)
    name = a.get("name") or a.get("target") or a.get("label")
    try:
        timeout_ms = int(a.get("timeoutms") or a.get("timeout_ms") or 10000)
    except (TypeError, ValueError):
        timeout_ms = 10000
    try:
        poll_ms = int(a.get("pollintervalms") or a.get("poll_interval_ms") or 200)
    except (TypeError, ValueError):
        poll_ms = 200

    wid, title = _resolve_hwnd_darwin(a)
    if wid is None:
        return {"status": "error", "error": "WaitForElement 必须提供窗口选择器。"}

    deadline = time.time() + timeout_ms / 1000.0
    while True:
        try:
            from .uia_darwin import inspect
            inspected = inspect(wid, max_depth=8, max_items=200, time_budget_ms=poll_ms)
            found = any(
                str(name).casefold() in str(e.get("name", "")).casefold()
                for e in inspected.get("elements", [])
            )
            if found:
                return {"status": "success", "result": f"元素 \"{name}\" 已出现。"}
        except Exception:
            pass
        if time.time() >= deadline:
            return {"status": "error", "error": f"等待超时（{timeout_ms}ms）：元素 \"{name}\" 未出现。"}
        time.sleep(poll_ms / 1000.0)