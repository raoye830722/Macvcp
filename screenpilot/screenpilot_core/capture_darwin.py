# -*- coding: utf-8 -*-
"""macOS 截图后端：Quartz CGWindowListCreateImage 实现。

与 capture.py 的函数契约对齐：
- capture_window_fallback(window_rect, ...) → (PIL.Image, capture_rect_dict, backend)
- capture_full_virtual_desktop(debug_log) → (image, virtual_rect_dict, backend)
- 额外提供 capture_window_by_id(window_id)：按窗口 ID 直接捕获内容，
  等价 Windows PrintWindow 语义（天然抗遮挡），由 host_darwin 调用。
- 坐标契约：所有 rect 一律逻辑点；返回图像为物理像素（Retina 2x），
  scale = img_width / rect_width，OCR 像素坐标换算时须除以 scale。
"""

from .geometry import Rect, get_virtual_screen_rect  # noqa: F401 (get_virtual_screen_rect 仅为兼容 re-export)


def _virtual_desktop_rect():
    """全部分辨率显示器逻辑坐标的并集矩形（原点=主显左上）。"""
    import Quartz
    try:
        err, displays, _count = Quartz.CGGetActiveDisplayList(32, None, None)
        if err == 0 and displays:
            union = None
            for d in displays:
                b = Quartz.CGDisplayBounds(d)
                r = Rect.from_ltrb(int(b.origin.x), int(b.origin.y),
                                   int(b.origin.x + b.size.width),
                                   int(b.origin.y + b.size.height))
                union = r if union is None else union.union(r)
            if union is not None:
                return union
    except Exception:
        pass
    main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return Rect.from_ltrb(int(main.origin.x), int(main.origin.y),
                          int(main.origin.x + main.size.width),
                          int(main.origin.y + main.size.height))


def _cgimage_to_pil(cgimg):
    """CGImage → PIL RGB Image（处理 BGRA 字节序与行对齐填充）。"""
    from PIL import Image
    import Quartz

    width = Quartz.CGImageGetWidth(cgimg)
    height = Quartz.CGImageGetHeight(cgimg)
    data_ref = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cgimg))
    buf = bytes(data_ref)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(cgimg)

    if bytes_per_row == width * 4:
        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    else:
        # 行尾有对齐填充：逐行重建（罕见路径，仅超宽对齐时触发）
        img = Image.new("RGBA", (width, height))
        for row in range(height):
            row_bytes = buf[row * bytes_per_row: row * bytes_per_row + width * 4]
            if len(row_bytes) < width * 4:
                break
            row_img = Image.frombuffer("RGBA", (width, 1), row_bytes, "raw", "BGRA", 0, 1)
            img.paste(row_img, (0, row))

    if img.mode != "RGB":
        rgb = Image.new("RGB", img.size, (0, 0, 0))
        rgb.paste(img, mask=img.split()[3])
        return rgb
    return img


def _capture_cg(rect=None, window_id=None, *, debug_log=None):
    """
    Quartz 捕获核心。

    rect+window_id=None → 全虚拟桌面；window_id → 单窗口内容；
    仅 rect → 屏幕区域。返回 (PIL.Image, capture_rect_logical_dict, scale)。
    """
    import Quartz

    if window_id is not None:
        cg_rect = Quartz.CGRectNull
        options = Quartz.kCGWindowListOptionIncludingWindow
        target = window_id
    else:
        if rect is None:
            cg_rect = Quartz.CGRectNull
        else:
            r = Rect.from_mapping(rect)
            cg_rect = Quartz.CGRectMake(r.left, r.top, r.width, r.height)
        options = Quartz.kCGWindowListOptionOnScreenOnly
        target = Quartz.kCGNullWindowID

    cg_img = Quartz.CGWindowListCreateImage(
        cg_rect,
        options,
        target,
        Quartz.kCGWindowImageBestResolution,
    )
    if cg_img is None:
        raise RuntimeError("CGWindowListCreateImage 未返回图像（屏幕录制权限或窗口已关闭）。")

    image = _cgimage_to_pil(cg_img)

    if window_id is not None:
        from . import windows_darwin
        info = windows_darwin.window_info(window_id)
        capture_rect = Rect.from_mapping(info["windowRect"])
    elif rect is not None:
        capture_rect = Rect.from_mapping(rect)
    else:
        capture_rect = _virtual_desktop_rect()

    scale = 1.0
    if capture_rect.width > 0:
        scale = image.width / float(capture_rect.width)

    if debug_log:
        debug_log(
            f"Quartz 截图成功 {image.size}: "
            f"capture_rect=({capture_rect.left},{capture_rect.top} "
            f"{capture_rect.width}×{capture_rect.height}) scale={scale:.2f}"
        )
    return image, capture_rect.to_dict(), scale


def capture_window_by_id(window_id, *, debug_log=None):
    """按窗口 ID 捕获窗口内容（抗遮挡，等价 PrintWindow）。

    返回 (image, capture_rect_dict, scale, backend)。
    """
    image, rect, scale = _capture_cg(window_id=int(window_id), debug_log=debug_log)
    return image, rect, scale, "quartz_window_capture"


def capture_window_fallback(window_rect, *, prefer_dxgi=True, debug_log=None):
    """契约兼容入口：按逻辑坐标区域捕获屏幕（macOS 无 DXGI/GDI 之分）。"""
    r = Rect.from_mapping(window_rect)
    if r.width <= 0 or r.height <= 0:
        raise ValueError(f"窗口尺寸无效: {r.width}x{r.height}")
    image, rect, _scale = _capture_cg(rect=r, debug_log=debug_log)
    return image, rect, "quartz_region"


def capture_full_virtual_desktop(debug_log=None):
    """截取完整虚拟桌面（含多屏），返回 (image, rect_dict, backend)。"""
    virtual = _virtual_desktop_rect()
    image, rect, _scale = _capture_cg(rect=None, debug_log=debug_log)
    if debug_log:
        debug_log(
            f"完整虚拟桌面截图成功 {image.size}: "
            f"({virtual.left},{virtual.top} {virtual.width}×{virtual.height})"
        )
    return image, virtual.to_dict(), "quartz_fullscreen"


def reset_capture_backend():
    """契约兼容：Quartz 无常驻会话，无需释放。"""
    return None