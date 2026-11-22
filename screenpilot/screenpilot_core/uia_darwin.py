# -*- coding: utf-8 -*-
"""macOS Accessibility (AX) 树探测、短期元素引用与语义动作。

契约对齐 uia.py：
- inspect(hwnd, ...) → {elements, visitedNodes, elapsedMs, truncated,
  truncatedReason, windowTitle}
- resolve_element(hwnd, selector, ...) → (ax_ref, info_dict)
- perform_action(hwnd, selector, action, value) → {action, pattern, element}
- 元素字典键与 uia.py 一致：elementId/controlType/name/boundingRect/
  clickablePoint/automationId/className/value/isEnabled/isOffscreen。

语义映射：
- AXButton→ButtonControl, AXTextField→EditControl, AXMenuItem→MenuItemControl,
  AXCheckBox→CheckBoxControl, AXLink→HyperlinkControl, AXStaticText→TextControl…
- 动作：invoke→AXPress, setValue→AXValue 写, toggle→AXToggle,
  select→AXSelected, focus→AXRaise（语义近似）。

已知边界（manifest macOS 注记同步）：
- AX 树比 UIA 稀疏：部分原生应用暴露控件有限；Chrome 系需先触发
  AXManualAccessibility（本模块对 Chromium 进程自动尝试）。
- Electron 应用 AX 支持良好。
"""

import hashlib
import json
import time

from .errors import ScreenPilotError

# ============================================================
# 类型映射：AXRole → UIA 风格 ControlType
# ============================================================

_AX_TO_CONTROL_TYPE = {
    "AXButton": "ButtonControl",
    "AXTextField": "EditControl",
    "AXTextArea": "EditControl",
    "AXStaticText": "TextControl",
    "AXMenuItem": "MenuItemControl",
    "AXMenuBarItem": "MenuItemControl",
    "AXMenu": "MenuControl",
    "AXMenuBar": "MenuBarControl",
    "AXCheckBox": "CheckBoxControl",
    "AXRadioButton": "RadioButtonControl",
    "AXPopUpButton": "ComboBoxControl",
    "AXComboBox": "ComboBoxControl",
    "AXLink": "HyperlinkControl",
    "AXImage": "ImageControl",
    "AXList": "ListControl",
    "AXTableRow": "DataItemControl",
    "AXTable": "TableControl",
    "AXTabGroup": "TabControl",
    "AXRadioButtonGroup": "RadioButtonControl",
    "AXSlider": "SliderControl",
    "AXProgressIndicator": "ProgressBarControl",
    "AXScrollBar": "ScrollBarControl",
    "AXGroup": "GroupControl",
    "AXWindow": "WindowControl",
    "AXToolbar": "ToolBarControl",
    "AXTabGroup": "TabControl",
    "AXOutline": "TreeControl",
    "AXTreeItem": "TreeItemControl",
    "AXListRow": "ListItemControl",
    "AXSwitch": "CheckBoxControl",
    "AXPopUpButton": "ComboBoxControl",
    "AXSearchField": "EditControl",
}

_TYPE_ALIASES = {
    "button": "AXButton",
    "edit": "AXTextField",
    "menuitem": "AXMenuItem",
    "checkbox": "AXCheckBox",
    "radiobutton": "RadioButtonControl",
    "combobox": "AXComboBox",
    "hyperlink": "AXLink",
    "listitem": "AXListRow",
    "treeitem": "AXTreeItem",
    "tabitem": "AXRadioButton",
    "slider": "AXSlider",
    "toolbar": "AXToolbar",
    "menubar": "AXMenuBar",
}

_INTERACTIVE_ROLES = {
    "AXButton", "AXTextField", "AXTextArea", "AXMenuItem", "AXMenuBarItem",
    "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXLink",
    "AXSlider", "AXSearchField", "AXSwitch", "AXToolbar", "AXMenuBar",
}

_ELEMENT_TTL_SECONDS = 120
_ELEMENT_CACHE_LIMIT = 1000
_element_cache = {}


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _normalize_type(value):
    """UIA 风格类型名 → AXRole（兼容筛选参数）。"""
    if not value:
        return None
    normalized = str(value).strip().lower().replace(" ", "").replace("control", "")
    if normalized in _TYPE_ALIASES:
        return _TYPE_ALIASES[normalized]
    ax_candidate = "AX" + normalized.capitalize()
    if ax_candidate in _AX_TO_CONTROL_TYPE:
        return ax_candidate
    return str(value)


def _runtime_id(ax_ref):
    """AX 引用无稳定 runtimeId，用 (pid, window, hash) 组合指纹。"""
    try:
        return [id(ax_ref) % 10**12]
    except Exception:
        return None


# ============================================================
# 元素缓存（TTL + 容量上限，对齐 uia.py 机制）
# ============================================================

def _cache_put(key, ax_ref):
    import time as _t
    _element_cache[key] = (ax_ref, _t.time() + _ELEMENT_TTL_SECONDS)
    if len(_element_cache) > _ELEMENT_CACHE_LIMIT:
        cutoff = _t.time() - _ELEMENT_TTL_SECONDS
        expired = [k for k, (_, exp) in _element_cache.items() if exp < cutoff]
        for k in expired:
            _element_cache.pop(k, None)


def _cache_get(key):
    import time as _t
    item = _element_cache.get(key)
    if not item:
        return None
    ax_ref, expires = item
    if _t.time() > expires:
        _element_cache.pop(key, None)
        return None
    return ax_ref


# ============================================================
# AX 树遍历
# ============================================================

def _ax_attr(element, name, default=None):
    """读取 AX 属性（pyobjc 正确签名：返回 (resultCode, value) 元组）。"""
    from ApplicationServices import AXUIElementCopyAttributeValue

    def _get():
        code, value = AXUIElementCopyAttributeValue(element, name, None)
        if code != 0 or value is None:
            return None
        return value

    value = _safe(_get, None)
    return value if value is not None else default


def _ax_children(element):
    value = _ax_attr(element, "AXChildren")
    try:
        return list(value) if value else []
    except TypeError:
        return []


def _ax_value_to_pair(value):
    """AXValueRef → (x, y) 或 (w, h)（AXValueGetValue 三参 out 姿势）。"""
    from ApplicationServices import AXValueGetType, AXValueGetValue

    def _get():
        t = AXValueGetType(value)
        ok, v = AXValueGetValue(value, t, None)
        if not ok:
            return None
        return v

    v = _safe(_get, None)
    if v is None:
        return None
    try:
        return float(v.x), float(v.y)
    except AttributeError:
        try:
            return float(v.width), float(v.height)
        except AttributeError:
            return None


def _ax_position_size(element):
    """读取 AXPosition/AXSize，返回 (x, y, w, h) 逻辑点。"""
    pos = _ax_attr(element, "AXPosition")
    size = _ax_attr(element, "AXSize")
    if pos is None or size is None:
        return None
    p = _ax_value_to_pair(pos)
    s = _ax_value_to_pair(size)
    if p is None or s is None:
        return None
    return int(p[0]), int(p[1]), int(s[0]), int(s[1])


def _element_info(hwnd, ax_ref, role, title, pid):
    """构造与 uia.py 契约一致的元素字典。"""
    from ApplicationServices import AXUIElementCopyAttributeValue

    ct = _AX_TO_CONTROL_TYPE.get(role, role or "UnknownControl")
    pos_size = _ax_position_size(ax_ref)
    value = _ax_attr(ax_ref, "AXValue")
    desc = _ax_attr(ax_ref, "AXDescription")
    enabled = _ax_attr(ax_ref, "AXEnabled")
    focused = _ax_attr(ax_ref, "AXFocused")

    name = str(title or desc or value or "")
    automation_id = _safe(lambda: hashlib.md5(
        f"{role}|{title}|{desc}".encode("utf-8")).hexdigest()[:12], None)

    info = {
        "elementId": None,  # 由调用方缓存后填入
        "controlType": ct,
        "axRole": role,
        "name": name,
        "boundingRect": {"x": 0, "y": 0, "width": 0, "height": 0},
        "clickablePoint": {"x": 0, "y": 0},
        "automationId": automation_id,
        "className": role or "",
        "value": str(value) if value is not None and not isinstance(value, (int, float)) else value,
        "isEnabled": bool(enabled) if enabled is not None else True,
        "isOffscreen": pos_size is None,
        "isFocused": bool(focused) if focused is not None else False,
    }
    if pos_size:
        x, y, w, h = pos_size
        info["boundingRect"] = {"x": x, "y": y, "width": w, "height": h}
        info["clickablePoint"] = {"x": x + w // 2, "y": y + h // 2}
    return info


def _ensure_chrome_accessibility(pid):
    """Chrome 系进程：尝试触发 AXManualAccessibility（幂等）。"""
    from .windows_darwin import process_name_by_hwnd  # 复用进程名查询（按 window id）
    # 实际按 pid 判断进程名
    try:
        import psutil
        pname = (psutil.Process(int(pid)).name() or "").lower()
        if "chrome" in pname or "chromium" in pname or "edge" in pname:
            from ApplicationServices import (
                AXUIElementCreateApplication,
                AXUIElementSetAttributeValue,
            )
            app = AXUIElementCreateApplication(int(pid))
            _safe(lambda: AXUIElementSetAttributeValue(
                app, "AXManualAccessibility", True), None)
    except Exception:
        pass


def inspect(hwnd, *, max_depth=5, max_items=50, control_type=None,
            include_noninteractive=False, time_budget_ms=3000):
    """
    AX 树探测（契约对齐 uia.inspect）。

    hwnd: CGWindowID；通过 kCGWindowOwnerPID 找到目标应用 AXUIElement。
    """
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
    )
    from .windows_darwin import window_info

    start = time.time()
    win_info = window_info(hwnd)
    pid = win_info["processId"]
    window_title = win_info["title"]

    _ensure_chrome_accessibility(pid)

    app = AXUIElementCreateApplication(int(pid))
    if app is None:
        raise ScreenPilotError(
            "UIA_INSPECT_FAILED",
            f"无法创建 pid:{pid} 的 AX 应用引用（辅助功能权限或进程已退出）。",
            retryable=True,
        )

    type_filter = _normalize_type(control_type)
    elements = []
    visited = [0]
    truncated = [False]
    deadline = start + time_budget_ms / 1000.0

    def walk(node, depth, path):
        if depth > max_depth or visited[0] >= max_items or time.time() > deadline:
            if visited[0] >= max_items:
                truncated[0] = True
            return
        visited[0] += 1
        role = _ax_attr(node, "AXRole") or ""
        title = _ax_attr(node, "AXTitle")
        children = _ax_children(node)
        interactive = role in _INTERACTIVE_ROLES
        if (include_noninteractive or interactive) and role:
            if type_filter is None or role == type_filter:
                info = _element_info(hwnd, node, role, title, pid)
                key = hashlib.md5(
                    f"{hwnd}|{path}|{role}|{title}".encode("utf-8")).hexdigest()[:16]
                _cache_put(key, node)
                info["elementId"] = f"ax-{key}"
                elements.append(info)
        for i, child in enumerate(children):
            walk(child, depth + 1, f"{path}/{i}")

    # 从应用的窗口开始遍历（_ax_attr 内部完成 (code, value) 解包）
    # 窗口优先于菜单栏（对齐 Windows InspectUI 体验），菜单栏排最后兜底
    app_children = _ax_children(app)
    windows = [c for c in app_children
               if (_ax_attr(c, "AXRole") or "") == "AXWindow"]
    menubars = [c for c in app_children
                if (_ax_attr(c, "AXRole") or "") == "AXMenuBar"]
    ordered = windows[:3] or app_children[:3]
    if not ordered:
        # 回退：从应用根开始
        walk(app, 0, "app")
    else:
        for wi, win in enumerate(ordered):
            walk(win, 0, f"win{wi}")
        for mi, mb in enumerate(menubars[:1]):
            walk(mb, 0, f"menubar{mi}")

    elapsed = int((time.time() - start) * 1000)
    return {
        "elements": elements,
        "visitedNodes": visited[0],
        "elapsedMs": elapsed,
        "truncated": truncated[0],
        "truncatedReason": "maxItems" if truncated[0] else None,
        "windowTitle": window_title,
    }


def resolve_element(hwnd, selector, *, time_budget_ms=2000):
    """
    按选择器解析元素（契约对齐 uia.resolve_element）。

    selector: {elementId | name | automationId | controlType, matchMode, index}
    返回 (ax_ref, info_dict)。
    """
    element_id = selector.get("elementId")
    if element_id:
        ax_ref = _cache_get(str(element_id).replace("ax-", ""))
        if ax_ref is None:
            raise ScreenPilotError(
                "ELEMENT_STALE",
                f"元素引用 {element_id} 已过期或不存在，请重新 InspectUI。",
                retryable=True,
            )
        role = _ax_attr(ax_ref, "AXRole") or ""
        info = _element_info(None, ax_ref, role,
                             _ax_attr(ax_ref, "AXTitle"), None)
        return ax_ref, info

    name = selector.get("name")
    automation_id = selector.get("automationId")
    control_type = selector.get("controlType")
    match_mode = str(selector.get("matchMode") or "exact").lower()
    index = int(selector.get("index") or 1)

    inspected = inspect(
        hwnd,
        max_depth=10,
        max_items=300,
        control_type=control_type,
        include_noninteractive=False,
        time_budget_ms=time_budget_ms,
    )
    candidates = []
    for e in inspected["elements"]:
        if automation_id and e.get("automationId") == automation_id:
            candidates.append(e)
            continue
        if name:
            ename = str(e.get("name") or "")
            if match_mode == "exact" and ename == str(name):
                candidates.append(e)
            elif match_mode == "contains" and str(name) in ename:
                candidates.append(e)
            elif match_mode == "startswith" and ename.startswith(str(name)):
                candidates.append(e)

    if not candidates:
        raise ScreenPilotError(
            "ELEMENT_NOT_FOUND",
            f"未找到匹配选择器的元素: {selector}",
            retryable=True,
        )
    if index > len(candidates):
        raise ScreenPilotError(
            "ELEMENT_NOT_FOUND",
            f"找到 {len(candidates)} 个匹配元素，但请求第 {index} 个。",
            retryable=True,
        )
    selected = candidates[index - 1]
    ax_ref = _cache_get(str(selected["elementId"]).replace("ax-", ""))
    if ax_ref is None:
        raise ScreenPilotError("ELEMENT_STALE", "元素引用已过期。", retryable=True)
    return ax_ref, selected


def perform_action(hwnd, selector, action, value=None):
    """
    语义动作（契约对齐 uia.perform_action）。

    invoke→AXPress / setValue→AXValue 写 / toggle→AXToggle /
    select→AXSelected 置真 / focus→AXRaise。
    """
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        kAXPressAction,
        kAXRaiseAction,
    )

    ax_ref, info = resolve_element(hwnd, selector)
    action_l = str(action or "invoke").lower()

    def _do(action_name, **kwargs):
        from ApplicationServices import AXUIElementPerformAction
        AXUIElementPerformAction(ax_ref, action_name)

    if action_l in ("invoke", "press", "click"):
        _do(kAXPressAction)
        pattern = "AXPress"
    elif action_l in ("setvalue", "set_value", "set"):
        # AXValue 写入
        from ApplicationServices import AXUIElementSetAttributeValue
        ok = _safe(lambda: AXUIElementSetAttributeValue(ax_ref, "AXValue", value), None)
        if ok is False:
            raise ScreenPilotError(
                "UIA_ACTION_FAILED",
                f"元素不支持 AXValue 写入（controlType={info['controlType']}）。",
            )
        pattern = "AXValue"
    elif action_l in ("toggle", "check"):
        from ApplicationServices import AXUIElementPerformAction
        AXUIElementPerformAction(ax_ref, "AXToggle")
        pattern = "AXToggle"
    elif action_l == "select":
        from ApplicationServices import AXUIElementSetAttributeValue
        _safe(lambda: AXUIElementSetAttributeValue(ax_ref, "AXSelected", True), None)
        pattern = "AXSelected"
    elif action_l in ("focus", "raise"):
        _do(kAXRaiseAction)
        pattern = "AXRaise"
    else:
        raise ScreenPilotError(
            "UIA_ACTION_FAILED",
            f"不支持的动作: {action}。可用: invoke/setValue/toggle/select/focus。",
        )

    return {
        "action": action_l,
        "pattern": pattern,
        "element": info,
    }