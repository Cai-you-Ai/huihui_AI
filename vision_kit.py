# -*- coding: utf-8 -*-
"""
微信 4.x 图像识别工具包（不依赖 UI 控件树）

统一约定：启动时开启 DPI 感知，全程使用「物理像素」坐标，
避免 200% 缩放环境下截图尺寸和点击坐标对不上。
"""

import ctypes
import time
from ctypes import wintypes

# ---- 必须在导入 pyautogui / PIL 之前开启 DPI 感知 ----
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

import pyperclip  # noqa: E402
import win32gui  # noqa: E402
import win32ui  # noqa: E402
from PIL import Image  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# PrintWindow 标志：2 = PW_RENDERFULLCONTENT，能抓 DirectX/GPU 渲染的窗口
PW_RENDERFULLCONTENT = 2

SW_RESTORE = 9

# 微信红（未读红点 / 发送按钮高亮）
RED_RANGES = [
    (200, 255, 40, 130, 40, 130),   # #FA5151 一类的红
    (200, 255, 50, 140, 20, 110),   # 偏橙的红
]


def enum_windows():
    out = []
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value
        if not (cls.startswith("Qt5") or "WeChat" in cls or "Weixin" in cls):
            return True
        tbuf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, tbuf, 256)
        title = tbuf.value
        if not title:
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        # 最小化的窗口 rect 会缩成小条，也要认出来（后面会自动还原）
        if (w > 300 and h > 300) or user32.IsIconic(hwnd):
            out.append({"hwnd": hwnd, "cls": cls, "title": title,
                        "rect": (r.left, r.top, r.right, r.bottom), "area": w * h})
        return True

    user32.EnumWindows(proc_type(cb), 0)
    return out


def find_wechat():
    """返回微信主窗口信息，找不到抛错"""
    wins = [w for w in enum_windows()
            if "微信" in w["title"] or "WeChat" in w["title"] or "Weixin" in w["title"]]
    if not wins:
        raise SystemExit("没找到微信窗口。请确认微信已打开、且未最小化到托盘。")
    return max(wins, key=lambda w: w["area"])


def activate(win):
    hwnd = win["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)
    # 移到屏幕内固定位置，保证截图区域有效
    w = win["rect"][2] - win["rect"][0]
    h = win["rect"][3] - win["rect"][1]
    user32.MoveWindow(hwnd, 100, 100, w, h, True)
    time.sleep(0.3)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    win["rect"] = (rect.left, rect.top, rect.right, rect.bottom)


def find_render_child(win):
    """找画内容的大子窗口（MMUIRenderSubWindowHW）
    返回 (hwnd, 屏幕rect) 或 None"""
    best = None

    def cb(hwnd, _):
        nonlocal best
        if not win32gui.IsWindowVisible(hwnd):
            return True
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w > 300 and h > 300 and (best is None or w * h > best[1]):
            best = (hwnd, w * h, r)
        return True

    try:
        win32gui.EnumChildWindows(win["hwnd"], cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return best[0::2] if best else None  # (hwnd, rect)


def _blank(img):
    """是不是空白/全黑帧。微信在切换会话的瞬间，PrintWindow 会返回一张空图，
    直接拿去 OCR 什么都读不到（这就是“点开却没读到标题”的根因）"""
    try:
        lo, hi = img.resize((48, 36)).convert("L").getextrema()
        return hi - lo < 10
    except Exception:  # noqa: BLE001
        return False


def grab(win, region=None):
    """抓图并对空白帧自动重试，最多 3 次"""
    last = None
    for i in range(4):
        try:
            img, offset = _grab_once(win, region)
        except RuntimeError:
            if i >= 3:
                raise
            time.sleep(0.4)
            continue
        if not _blank(img):
            return img, offset
        last = (img, offset)
        time.sleep(0.4)
    if last is None:
        raise RuntimeError("截图失败：PrintWindow 不可用")
    return last


def _grab_once(win, region=None):
    """抓微信内容区（优先对渲染子窗口 PrintWindow，失败退回主窗口）。
    返回 (PIL.Image, offset) —— offset 是内容区相对主窗口的偏移"""
    child = find_render_child(win)
    targets = []
    if child:
        targets.append(child)
    targets.append((win["hwnd"], win["rect"]))

    for hwnd, rect in targets:
        l, t, r, b = rect
        w, h = r - l, b - t
        try:
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)

            ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
            info = bmp.GetInfo()
            data = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                   data, "raw", "BGRX", 0, 1)

            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

            if not ok:
                continue

            offset = (l - win["rect"][0], t - win["rect"][1])
            if region:
                img = img.crop((region[0] - offset[0], region[1] - offset[1],
                                region[2] - offset[0], region[3] - offset[1]))
                offset = (region[0], region[1])
            return img, offset
        except Exception:  # noqa: BLE001
            continue

    raise RuntimeError("截图失败：PrintWindow 不可用")


def is_red(px):
    r, g, b = px[0], px[1], px[2]
    for rmin, rmax, gmin, gmax, bmin, bmax in RED_RANGES:
        if rmin <= r <= rmax and gmin <= g <= gmax and bmin <= b <= bmax:
            return True
    return False


def find_red_rows(img, min_count=6):
    """按行统计红色像素，返回 [(y_start, y_end, count, x_center)]，坐标相对传入图像"""
    w, h = img.size
    px = img.load()
    rows = []
    for y in range(0, h, 2):
        xs = [x for x in range(0, w, 2) if is_red(px[x, y])]
        if len(xs) >= min_count:
            rows.append((y, len(xs), sum(xs) // len(xs)))

    if not rows:
        return []

    # 把连续的 y 合并成块
    blocks = []
    cur = [rows[0]]
    for item in rows[1:]:
        if item[0] - cur[-1][0] <= 12:
            cur.append(item)
        else:
            blocks.append(cur)
            cur = [item]
    blocks.append(cur)

    res = []
    for blk in blocks:
        ys = [b[0] for b in blk]
        counts = [b[1] for b in blk]
        xc = [b[2] for b in blk]
        res.append({
            "y1": min(ys), "y2": max(ys),
            "y_center": (min(ys) + max(ys)) // 2,
            "x_center": sum(xc) // len(xc),
            "count": sum(counts),
        })
    return res


def foreground_hwnd():
    return user32.GetForegroundWindow()


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def user_idle_seconds():
    """用户多久没碰鼠标键盘了（秒）。用来判断人是不是正在用电脑，
    人还在的时候不要抢着替他回话。"""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return max(0.0, (kernel32.GetTickCount() - lii.dwTime) / 1000.0)


def restore_focus(hwnd):
    """把前台还给之前的窗口"""
    if not hwnd or hwnd == user32.GetForegroundWindow():
        return True
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


def bring_front(win):
    """把微信提到最前面（不改变窗口大小和位置）。
    窗口的线程和当前前台线程不同时，SetForegroundWindow 会被系统忽略，
    所以先 AttachThreadInput 借用一下前台线程的输入状态"""
    hwnd = win["hwnd"]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
        if user32.GetForegroundWindow() == hwnd:
            return True
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_wx = user32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if tid_fg and tid_wx and tid_fg != tid_wx:
            attached = bool(user32.AttachThreadInput(tid_fg, tid_wx, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(tid_fg, tid_wx, False)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def is_minimized(win):
    return bool(user32.IsIconic(win["hwnd"]))


def ensure_onscreen(win, normalize=False):
    """保证窗口已还原、在屏幕内、且尺寸不会小到让布局比例失效。
    normalize=True 时（启动时）直接把窗口统一成标准大小，
    后面运行中即使被拖大也无所谓，坐标是动态测的"""
    hwnd = win["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if normalize:
        nw, nh = min(1420, sw - 120), min(970, sh - 120)
        user32.MoveWindow(hwnd, 80, 80, nw, nh, True)
        time.sleep(0.6)
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    elif w < 1250 or h < 800:
        nw, nh = min(1560, sw - 120), min(1000, sh - 120)
        user32.MoveWindow(hwnd, 80, 80, nw, nh, True)
        time.sleep(0.5)
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    elif rect.bottom > sh + 40 or rect.right > sw + 40 or rect.left + w < 300:
        user32.MoveWindow(hwnd, 100, 100, w, h, True)
        time.sleep(0.35)
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    win["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
    return True


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def _lp(x, y):
    return (int(y) << 16) | (int(x) & 0xFFFF)


def bg_click(win, x, y, restore=True):
    """后台点击：直接给微信主窗口投递鼠标消息，坐标用「内容图坐标」。
    不移动真实光标、不需要窗口在前台，但微信自身会被激活一下，
    所以点击后把前台还给原来的窗口。"""
    prev = user32.GetForegroundWindow()
    child = find_render_child(win)
    ox = oy = 0
    if child:
        ox = child[1][0] - win["rect"][0]
        oy = child[1][1] - win["rect"][1]
    hwnd = win["hwnd"]
    px, py = int(x) + ox, int(y) + oy
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, _lp(px, py))
    time.sleep(0.04)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _lp(px, py))
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, _lp(px, py))
    if restore:
        time.sleep(0.35)
        restore_focus(prev)


def click(x, y):
    """绝对物理坐标点击"""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.1)


def click_in_window(win, x, y):
    """内容图坐标 → 屏幕绝对坐标后点击。
    和 bg_click 一样要补偿「内容子窗口相对主窗口」的偏移，
    否则内容区不是从主窗口 (0,0) 开始时（DPI/布局变化），点输入框会错位。"""
    l, t, _, _ = win["rect"]
    child = find_render_child(win)
    ox = oy = 0
    if child:
        ox = child[1][0] - l
        oy = child[1][1] - t
    click(l + x + ox, t + y + oy)


def set_clipboard(text):
    pyperclip.copy(text)


def press_ctrl_v():
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_V = 0x56
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)


def press_enter():
    KEYEVENTF_KEYUP = 0x0002
    VK_RETURN = 0x0D
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)


def send_text(text):
    """剪贴板 + Ctrl+V，支持中文和换行"""
    set_clipboard(text)
    time.sleep(0.15)
    press_ctrl_v()
