# -*- coding: utf-8 -*-
"""
微信 4.x 图像识别自动回复（最终版）

原理：
  1. PrintWindow 抓微信渲染子窗口（MMUIRenderSubWindowHW）的像素
  2. 会话列表里按颜色找"未读红点"，点击进入对应会话
  3. OCR 聊天区，结合绿色气泡位置判断最后一条消息是不是对方发的
  4. 是的话：点输入框 -> 剪贴板粘贴文案 -> 回车发送

注意：
  - 运行期间鼠标键盘会被脚本占用，建议放着别动
  - 微信窗口别最小化（脚本会自动把它移到屏幕内固定位置）
  - 深色/浅色模式都按绿色气泡判断，微信改配色需要重新采样

用法：
  python wechat_vision_auto_reply.py --dry-run          # 演练
  python wechat_vision_auto_reply.py --reply "我在忙，稍后回你"
  python wechat_vision_auto_reply.py --once             # 只扫一轮
"""

import argparse
import json
import logging
import os
import re
import sys
import time

import ai_reply
import vision_kit as vk

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "wx_auto_state.json")

REPLY_PREFIX = "[慧慧] "  # 每条回复前面都带上，一眼能看出是助手发的

# 回复时才短暂把微信提到前台，发完立刻把前台还给原来的窗口，
# 平时不打扰（用户可能正在干别的事）
KEEP_FOREGROUND = False
# 同一个会话两次回复之间的最小间隔，比 --cooldown 更硬，防止连环回自己
MIN_GAP = 15
# 已经打开着的那个会话，两次回复之间至少隔这么久
CUR_CHAT_GAP = 25
# 频率熔断：同一个会话 10 分钟内最多回几次；所有会话 5 分钟内总共最多回几次。
# 万一又出现“把自己当新消息”之类的死循环，到上限就闭嘴，不会刷屏。
# 原值 4/8 偏低，正常聊天也会被拦（日志里出现过连跳 6 次），调到 6/12
PER_CHAT_LIMIT = 6
GLOBAL_LIMIT = 12

_REPLY_LOG = []          # [(时间, 会话key)] 最近 10 分钟的回复记录


def reply_rate_ok(key):
    now = time.time()
    while _REPLY_LOG and now - _REPLY_LOG[0][0] > 600:
        _REPLY_LOG.pop(0)
    if sum(1 for t, _ in _REPLY_LOG if now - t < 300) >= GLOBAL_LIMIT:
        return False, "5 分钟内已经回了 %d 条，先歇一歇（防刷屏熔断）" % GLOBAL_LIMIT
    n = sum(1 for t, k in _REPLY_LOG if k == key)
    if n >= PER_CHAT_LIMIT:
        return False, "「%s」10 分钟内已回 %d 条，先不回了" % (key[:10], n)
    return True, ""


def _is_self_text(t):
    """这条是不是我们自己发的。
    我们发出的每条都带「[慧慧]」开头，OCR 常认成 【慧慧] / [慧慧］ 之类，
    所以只认「带括号的慧慧」，不认单独出现的慧慧（对方也可能提到慧慧）"""
    head = (t or "")[:20]
    if re.search(r"[\[【（(〔]?\s*慧\s*慧\s*[\]】）)〕]", head):
        return True
    return "我是慧慧" in (t or "")[:24]

# 对方按 1 之后回这句，然后只记录、不再用 AI 回
TASK_PROMPT = "请将事务列在这条消息下面"
TASK_FILE = os.path.join(HERE, "wx_tasks.json")

DEFAULT_REPLY = ("你好呀~我是慧慧，朱恩锐的AI助手。他现在不在，该微信由我代理，"
                 "有事请按1，想和慧慧聊天请按2。如果有急事请直接call him 19113350934。")
POLL_INTERVAL = 10      # 每轮扫描间隔（秒）
COOLDOWN = 180          # 同一会话回复冷却（秒）
RESET_MIN = 30          # 对方隔了这么久才再开口，就重新回开场白（分钟）
IDLE_GUARD = 0          # 0 = 不看鼠标，只靠“输入框里有没有字”判断你在不在打字
EXCLUDE = ["微信团队", "订阅号", "服务号", "微信支付", "腾讯新闻",
           "公众号", "QQ邮箱", "文件传输助手", "微信支付爱心餐",
           "外卖", "美团", "饿了么", "红包", "优惠", "券", "会员",
           "通知", "客服", "小助手", "官方", "活动", "福利", "抽奖",
           "京东", "淘宝", "拼多多", "山姆", "银行", "快递", "运营商"]

# 名字/标题里带 @ 的基本是企业微信或服务号，一律不回
EXCLUDE_CHARS = ("@",)

# 消息内容里出现这些词的，大概率是广告/群发，不回
MSG_SKIP = ("领取", "点击", "小程序", "优惠", "红包", "退订", "活动",
            "抽奖", "限时", "秒杀", "立即", "包邮", "进货", "代理")

# 群聊一律不回复（除非显式 --allow-group）
# 群聊名字特征词。保守起见只放明确指向「多人聚合」的词，
# 避免把单聊昵称里带「家」「队」之类误判成群。
GROUP_WORDS = ("群", "群聊", "Group", "group", "GROUP",
               "讨论组", "聊天室")

# ---- 布局参数（内容图坐标按窗口尺寸比例换算）----
LIST_XR = (0.028, 0.307)  # 会话列表横向范围（占窗口宽度比例）
LIST_Y0 = 0.08            # 列表起始 y 比例（跳过搜索框）
CHAT_XR = 0.289           # 聊天面板左边界（比例）
TITLE_TOP = 40            # 标题栏顶部（像素，固定不随窗口高度变）
TITLE_BOT = 150           # 标题栏底部 = 聊天区顶部（像素）
INPUT_H = 290             # 输入区总高度（像素，从窗口底部往上算）
INPUT_XR = 0.637          # 输入框点击 x 比例；y 用固定：离底部 160px
ROW_XR = 0.12             # 点击会话行的横向位置（比例）
SELF_GREEN = lambda px: px[1] > 150 and px[0] < 120 and 60 < px[2] < 210

LOG_DIR = os.path.join(HERE, "logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:  # noqa: BLE001
    pass
LOG_PATH = os.path.join(LOG_DIR, "wx_auto_%s.log" % time.strftime("%Y%m%d"))

_handlers = []
if sys.stderr is not None:        # 用 pythonw 跑的时候没有控制台
    _handlers.append(logging.StreamHandler())
try:
    _handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers or None,
)
log = logging.getLogger("wx-vision")

# pythonw 崩溃时没有任何窗口提示，把崩溃现场写到文件里方便排查
try:
    import faulthandler
    faulthandler.enable(open(os.path.join(HERE, "logs", "fault.log"), "a"),
                        all_threads=True)
except Exception:  # noqa: BLE001
    pass

_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def ocr_img(img):
    res, _ = get_ocr()(img)
    out = []
    for box, text, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({
            "text": text,
            "x1": int(min(xs)), "x2": int(max(xs)),
            "y1": int(min(ys)), "y2": int(max(ys)),
            "y_center": int((min(ys) + max(ys)) / 2),
        })
    return out


CHAT_LEFT_DEFAULT = 605   # 本机微信列表面板宽度（固定像素，不随窗口缩放）


def chat_left(img):
    """实测会话列表和聊天面板的分界 x。
    列表宽度接近固定像素、不随窗口等比缩放，比例值在不同窗口大小下
    会错位。用列颜色跳变现场测，但聊天区内容（气泡/图片）会干扰，
    所以只在合理范围内采信，否则用默认值"""
    w, h = img.size
    px = img.load()
    y0, y1 = int(h * 0.15), int(h * 0.85)

    def col(x):
        r = g = b = n = 0
        for y in range(y0, y1, 10):
            p = px[x, y]
            r += p[0]; g += p[1]; b += p[2]; n += 1
        return (r / n, g / n, b / n)

    prev = col(int(w * 0.18))
    for x in range(int(w * 0.20), int(w * 0.72), 4):
        c = col(x)
        d = sum(abs(a - b) for a, b in zip(c, prev))
        # 明显变暗，且右边一小段都保持暗 → 这就是分界线
        if d > 40 and sum(c) < sum(prev):
            right = [col(x + dd) for dd in (24, 56, 88) if x + dd < w - 2]
            if right and all(sum(rc) < sum(prev) for rc in right):
                if CHAT_LEFT_DEFAULT - 40 <= x <= CHAT_LEFT_DEFAULT + 40:
                    return x
                return CHAT_LEFT_DEFAULT
        prev = c
    return CHAT_LEFT_DEFAULT


def geo(img):
    """按图片尺寸算布局：会话列表 x 范围、聊天面板左边界、标题高度"""
    w, h = img.size
    cx = chat_left(img) or int(w * CHAT_XR)
    return (int(w * LIST_XR[0]), int(w * LIST_XR[1]),
            int(cx), TITLE_BOT)


def row_name(img, y_center):
    """OCR 会话列表某一行，拿会话名"""
    x1, x2, _, _ = geo(img)
    crop = img.crop((x1, max(0, y_center - 15), x2, y_center + 75))
    blocks = [b for b in ocr_img(crop)
              if b["text"].strip() and not b["text"].strip().isdigit()]
    if not blocks:
        return None
    top = min(b["y1"] for b in blocks)
    for b in blocks:
        if b["y1"] - top < 12:      # 第一行 = 昵称
            return b["text"][:16]
    return blocks[0]["text"][:16]


def list_rows(img):
    """OCR 会话列表，返回每行的 (会话名, 行中心y)"""
    x1, x2, _, _ = geo(img)
    y0 = int(img.size[1] * LIST_Y0)
    y1 = int(img.size[1] * 0.97)
    name_x_limit = int((x2 - x1) * 0.57)
    blocks = [b for b in ocr_img(img.crop((x1, y0, x2, y1)))
              if b["x1"] < name_x_limit
              and not (b["text"].strip().isdigit() and b["x1"] < (x2 - x1) // 2)]
    blocks.sort(key=lambda b: b["y1"])
    rows = []
    for i, b in enumerate(blocks):
        prev_close = i > 0 and b["y1"] - blocks[i - 1]["y1"] < 60
        if prev_close:          # 同一行的预览文字，跳过
            continue
        rows.append((b["text"][:16], y0 + (b["y1"] + b["y2"]) // 2))
    return rows


def _red_runs(px, w, h, x, y):
    """从 (x,y) 向四方向走，统计四个方向上连续红色的长度（像素）。
    从离开数字 10px 处起步，避开数字本身的白色。"""
    runs = {}
    for name, dx, dy in (("up", 0, -1), ("down", 0, 1),
                         ("left", -1, 0), ("right", 1, 0)):
        run = 0
        cx, cy = x + dx * 10, y + dy * 10
        for _ in range(40):
            if not (0 <= cx < w and 0 <= cy < h):
                break
            if vk.is_red(px[cx, cy]):
                run += 1
                cx += dx * 2
                cy += dy * 2
            else:
                break
        runs[name] = run * 2
    return runs


def _find_badges(sub):
    """在头像右上角区域找未读角标：红色小圆 + 白色数字。
    判据：白色像素四周被红色包着，但红色范围很小（角标只有 30px 左右，
    红色头像里的白色图案四周的红延伸很远），且上/右方向很快露出深色背景。
    这样角标哪怕和红头像粘成一坨也能找出来。返回 [(x, y)]（sub 坐标）。"""
    px = sub.load()
    w, h = sub.size
    # 头像固定在列表左侧：侧边栏约 130px，头像区在 150~300px 之间，
    # 不随窗口宽度变，所以用固定像素范围（0.2~0.58 倍分界线宽度都能盖住）
    x_lo, x_hi = int(w * 0.2), int(w * 0.58)
    hits, taken = [], set()
    for y in range(6, h - 6):
        for x in range(x_lo, x_hi):
            p = px[x, y]
            if not (p[0] > 210 and p[1] > 210 and p[2] > 210):
                continue
            k = (x // 20, y // 20)
            if k in taken:
                continue
            # 快速预筛：紧邻 12px 里得有红
            nr = 0
            for yy in (-6, -3, 0, 3, 6):
                for xx in (-6, -3, 0, 3, 6):
                    if vk.is_red(px[min(w - 1, max(0, x + xx)),
                                    min(h - 1, max(0, y + yy))]):
                        nr += 1
            if nr < 6:
                continue
            runs = _red_runs(px, w, h, x, y)
            v, hz = runs["up"] + runs["down"], runs["left"] + runs["right"]
            total = v + hz
            if v > 64 or hz > 64 or total < 16:
                continue
            # 44px 见方里红色要占三成以上（把白字混在花头像里的排除掉）
            tot = red = 0
            for yy in range(max(0, y - 22), min(h, y + 22), 2):
                for xx in range(max(0, x - 22), min(w, x + 22), 2):
                    tot += 1
                    if vk.is_red(px[xx, yy]):
                        red += 1
            if red / max(1, tot) < 0.3:
                continue
            taken.add(k)
            hits.append((x, y))

    # 先按距离并成组（同一角标会出好几个白点），再逐组用 OCR 验证里面是数字
    hits.sort()
    groups = []
    for x, y in hits:
        for g in groups:
            if abs(g[0][0] - x) <= 26 and abs(g[0][1] - y) <= 26:
                g.append((x, y))
                break
        else:
            groups.append([(x, y)])

    out = []
    for g in groups:
        x, y = g[0]

        def badge_red_ratio():
            """微信角标红固定是 #FA5151 左右，精确匹配它。
            红色头像（比如 (228,68,68)）差得够远，不会误中"""
            n = m = 0
            for yy in range(max(0, y - 14), min(h, y + 15)):
                for xx in range(max(0, x - 14), min(w, x + 15)):
                    p = px[xx, yy]
                    n += 1
                    if 240 <= p[0] <= 255 and 66 <= p[1] <= 100 \
                            and 66 <= p[2] <= 100:
                        m += 1
            return m / max(1, n)

        rr = badge_red_ratio()
        box = sub.crop((max(0, x - 20), max(0, y - 20),
                        min(w, x + 20), min(h, y + 20)))
        big = box.resize((box.width * 4, box.height * 4))
        texts = [b["text"].strip() for b in ocr_img(big)]
        is_digit = any(t and len(t) <= 3
                       and sum(c.isdigit() for c in t) / len(t) >= 0.5
                       for t in texts)
        # 主判据：角标红占比；OCR 认出数字作为兜底（主题色变化时）
        if rr >= 0.35 or (is_digit and rr >= 0.15):
            out.append((x, y))
    return out


def find_unread(img, with_names=True):
    """找未读红点行，返回 [{y, x, count, name}]（y 为该行中心）
    with_names=False 时只做像素扫描不跑 OCR，可以做到「秒点」，
    避免截图和点击之间隔太久、列表已经重排。
    扫描范围用实测分界线：头像和角标是固定像素位置，不随窗口宽度缩放"""
    w = img.size[0]
    x2 = chat_left(img) or int(w * LIST_XR[1])
    y0 = int(img.size[1] * LIST_Y0)
    y1 = int(img.size[1] * 0.97)
    sub = img.crop((0, y0, x2, y1))
    names = list_rows(img) if with_names else []

    out = []
    for bx, by in _find_badges(sub):
        cy = y0 + by
        # 直接用这一行的 OCR 结果认名字，比在整列里找“最近的”更准
        name = row_name(img, cy) or ""
        # 角标在头像右上角，比行中心偏上，往下挪一点保证点在行内
        row_y = cy + int(img.size[1] * 0.03)
        best, bd = None, 1e9
        for nm, ny in names:
            d = abs(ny - (cy + 18))
            if d < bd:
                best, bd = (nm, ny), d
        if best is not None and bd <= 40:
            row_y = best[1]
            if not name:
                name = best[0]
        out.append({"y": row_y, "x": bx, "count": 1,
                    "name": name or "未知"})
    return out


UI_WORDS = ("长按鼠标", "语音输入", "发送", "按住说话", "输入框")

# 预览文字检测：默认关闭。它和 scan_round 末尾的「当前会话检查」重复，
# 且 OCR 抖动会让它反复把同一行判成新消息、反复点开刷屏（实测）。
# 只有当红点识别长期失灵时才考虑打开。
ENABLE_PREVIEW_DETECT = False

_PREVIEW_SEED_DONE = [False]
_PREVIEWS = {}


def list_previews(img):
    """OCR 会话列表，返回每行 (会话名, 预览文字, 行中心y)。
    预览文字 = 这个会话的最后一条消息。消息在聊天窗口开着的时候进来，
    微信会直接标成已读、永远不出红点，只有预览文字能暴露它"""
    x1, x2, _, _ = geo(img)
    y0 = int(img.size[1] * LIST_Y0)
    y1 = int(img.size[1] * 0.97)
    lw = x2 - x1
    time_pat = re.compile(r"^(\d{1,2}[:：]\d{2}|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日)$")
    blocks = [b for b in ocr_img(img.crop((x1, y0, x2, y1)))
              if b["text"].strip()
              and not any(k in b["text"] for k in UI_WORDS)
              and not time_pat.match(b["text"].strip())
              and not (b["text"].strip().isdigit() and b["x1"] < 90)]
    blocks.sort(key=lambda b: b["y1"])
    rows = []
    for b in blocks:
        if rows and b["y1"] - rows[-1]["y1"] < 90:
            rows[-1]["blocks"].append(b)
        else:
            rows.append({"y1": b["y1"], "blocks": [b]})
    out = []
    for r in rows:
        r["blocks"].sort(key=lambda b: b["y1"])
        name_b = r["blocks"][0]              # 第一行是会话名
        prevs = r["blocks"][1:]              # 下面是预览文字
        if not prevs:
            continue
        name = name_b["text"][:16]
        preview = " ".join(b["text"] for b in prevs).strip()
        out.append((name, preview, y0 + name_b["y1"] + 45))
    return out


def preview_suspects(img):
    """对比每行的预览文字，找出「没有红点但确实来了新消息」的会话。
    首轮只记基线不报警（启动时预览和上次比可能有 OCR 误差）"""
    if not _PREVIEW_SEED_DONE[0]:
        for name, prev, _ in list_previews(img):
            _PREVIEWS[norm_key(name)] = prev
        _PREVIEW_SEED_DONE[0] = True
        return []
    out = []
    for name, prev, y in list_previews(img):
        key = norm_key(name)
        if not key or not prev:
            continue
        old = _PREVIEWS.get(key)
        _PREVIEWS[key] = prev
        if old is None or _similar(old, prev):
            continue
        if _is_self_text(prev):          # 预览变成我们发的 → 不是新消息
            continue
        out.append({"y": y, "name": name})
    return out


def last_message_owner(img):
    """判断聊天区最后一条消息是谁发的：'other' / 'self' / None。
    消息从下往上堆，只裁底部最近一段，OCR 快好几倍"""
    w, h = img.size
    _, _, cx, th = geo(img)

    def _scan(chat):
        cw, ch = chat.size
        blocks = ocr_img(chat)
        # 裁剪时已经切掉输入区，这里只滤 UI 词
        blocks = [b for b in blocks
                  if b["text"].strip()
                  and not any(k in b["text"] for k in UI_WORDS)
                  and b["x1"] < cw - 115]   # 最右边是自己头像的位置，头像素材上的字会被 OCR 认出来
        if not blocks:
            return None, None
        last = max(blocks, key=lambda b: b["y2"])

        # 绿色气泡最低出现位置
        px = chat.load()
        cw, ch = chat.size
        y_green = -1
        for y in range(ch - 1, max(1, ch // 20), -3):
            cnt = sum(1 for x in range(0, cw, 6) if SELF_GREEN(px[x, y]))
            if cnt >= 8:
                y_green = y
                break

        owner = "other"
        if y_green > 0 and y_green > last["y2"] - 130:
            owner = "self"
        if _is_self_text(last["text"]):
            owner = "self"
        return owner, last["text"]

    y_bot = h - INPUT_H - 10
    y_top = y_bot - 330
    owner, text = _scan(img.crop((cx, y_top, w, y_bot)))
    if owner is None:               # 底部没读到（消息很少时），退回全区
        owner, text = _scan(img.crop((cx, TITLE_BOT, w, y_bot)))
    return owner, text


SYS_HINTS = ("撤回了一条消息", "以下为新消息", "你已添加了", "现在可以开始聊天",
             "以上是打招呼的内容", "拍了拍", "加入了群聊", "开启了朋友验证",
             "对方正在输入", "消息已发出，但被对方拒", "已成为好友", "推荐了",
             "该消息类型暂不支持", "引用了",
             # 微信自己弹的操作提示，不是对方说的话（实测「重新编辑」和
             # 英文客户端的撤回提示曾被当成新消息回复，很尴尬）
             "重新编辑", "You recalled a message", "recalled a message",
             "消息已撤回", "修改了消息",
             # 换网络/掉线时微信会弹这些，别当成对方说的话去回
             "网络连接", "网络异常", "网络不可用", "连接中", "正在连接",
             "微信已断开", "请检查网络", "登录已过期", "在其他设备登录",
             "已退出登录")

_TIME_PATTERNS = (
    r"^\d{1,2}[:：]\d{2}$",
    r"^(上午|下午|中午|晚上|凌晨|早上)\s*\d{1,2}[:：]\d{2}$",
    r"^(昨天|前天|今天|星期[一二三四五六日天]|周[一二三四五六日天])"
    r"\s*(\d{1,2}[:：]\d{2})?$",
    r"^\d{4}年\d{1,2}月\d{1,2}日.*$",
    r"^\d{1,2}月\d{1,2}日.*$",
)


def _is_time_text(t):
    t = (t or "").strip()
    return any(re.match(p, t) for p in _TIME_PATTERNS)


def _green_buckets(px, w, h, step=4, size=20):
    """按行分桶收集绿色气泡像素，后面判断每条消息归属用"""
    buckets = {}
    for y in range(0, h, step):
        for x in range(0, w, step):
            if SELF_GREEN(px[x, y]):
                buckets.setdefault(y // size, []).append((x, y))
    return buckets


def _block_owner(buckets, blk, size=20):
    """绿色气泡横跨这段文字的大部分 → 是我发的；否则是对方发的"""
    y1, y2 = blk["y1"] - 8, blk["y2"] + 8
    xs = []
    for k in range(max(0, y1 // size), y2 // size + 1):
        for x, y in buckets.get(k, ()):
            if y1 <= y <= y2:
                xs.append(x)
    if len(xs) < 8:
        return "other"
    span = max(1, blk["x2"] - blk["x1"])
    cover = (min(blk["x2"], max(xs)) - max(blk["x1"], min(xs))) / span
    return "self" if cover >= 0.6 else "other"


def chat_messages(img, maxn=8):
    """把聊天区拆成一条条消息，返回 [{text, owner, y1, y2}]（时间正序）
    owner: 'self' 是我发的，'other' 是对方发的"""
    w, h = img.size
    _, _, cx, th = geo(img)
    chat_h = h - th
    y_bot = h - INPUT_H - 10           # 消息区底部（输入区上沿）
    y_top = y_bot - 330                # 只取最近几条，OCR 快
    if y_top < TITLE_BOT:
        y_top = TITLE_BOT
    chat = img.crop((cx, y_top, w, y_bot))
    cw, ch = chat.size
    blocks = []
    for b in ocr_img(chat):
        t = b["text"].strip()
        if not t:
            continue
        if any(k in t for k in UI_WORDS):
            continue
        if _is_time_text(t) or any(k in t for k in SYS_HINTS):
            continue
        if b["x1"] > cw - 115:
            # 最右边是自己头像：头像照片里如果有字（比如照片素材上的中文），
            # 会被 OCR 认出来贴在我方气泡旁边，被当成对方说的话
            continue
        blocks.append(dict(b))
    if not blocks:
        return []
    blocks.sort(key=lambda b: b["y1"])

    # 归属判定要在拼行之前做：一旦把两边的文字拼成一行，
    # 「我发的」和「他发的」就分不开了，会把自己的话当成新消息再回一遍
    buckets = _green_buckets(chat.load(), cw, ch)
    for b in blocks:
        b["owner"] = "self" if _is_self_text(b["text"]) \
            else _block_owner(buckets, b)

    # 同一行被 OCR 拆成几块，先横向拼回一行（只拼同一侧的文字）
    lines = []
    for b in blocks:
        if lines and b["owner"] == lines[-1]["owner"] \
                and b["y1"] <= lines[-1]["y2"] - 2 \
                and b["x1"] <= lines[-1]["x2"] + 60:
            p = lines[-1]
            p["text"] += " " + b["text"]
            p["x2"] = max(p["x2"], b["x2"])
            p["y2"] = max(p["y2"], b["y2"])
        else:
            lines.append(b)

    msgs = []
    for b in lines:
        owner = b["owner"]
        if msgs and owner == msgs[-1]["owner"] and b["y1"] - msgs[-1]["y2"] < 10:
            msgs[-1]["text"] += " " + b["text"]          # 同一气泡内换行
            msgs[-1]["y2"] = b["y2"]
        else:
            msgs.append({"text": b["text"].strip(), "owner": owner,
                         "y1": b["y1"], "y2": b["y2"]})
    return msgs[-maxn:]


def norm_key(name):
    """会话名归一化，用作冷却键（OCR 每次标点会飘）"""
    import re
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", name)[:8]


def is_group_name(name):
    return any(w in name for w in GROUP_WORDS)


def chat_title(img):
    """聊天面板顶部标题（群聊会带 (36) 这种人数）。
    窗口大小变化时布局比例会漂，先裁标题常在的一小块快读（~0.4s），
    读不到再退回全宽扫描（慢但保底）。
    注意：群人数在标题末尾，绝不能按 x 距离截断，否则长群名会丢掉人数、
    导致群聊被漏判成单聊——这里把标题区整段都读进来。"""
    w, h = img.size

    def _read(crop, cxmin):
        blocks = [b for b in ocr_img(crop) if len(b["text"].strip()) > 1
                  and not _is_time_text(b["text"])
                  and "搜索" not in b["text"]
                  and b["y1"] >= 42          # 排除窗口控制按钮那排图标（裁剪坐标）
                  and b["x1"] >= cxmin]
        if not blocks:
            return ""
        # 标题栏只取最上面一行的文字块（人数可能单独成块在右侧），
        # 按 x 排序全部拼上，不再用 <300 的距离过滤
        blocks.sort(key=lambda b: b["x1"])
        return " ".join(b["text"].strip() for b in blocks)

    x0 = geo(img)[2] + 4
    t = _read(img.crop((x0, TITLE_TOP, x0 + 700, TITLE_BOT)), x0 - 10)
    if t:
        return t
    return _read(img.crop((0, TITLE_TOP, w, TITLE_BOT)), x0 - 10)


def looks_group_title(t):
    """群聊标题判据：标题末尾带成员人数，如「相亲相爱一家人 (36)」。
    兼容全角/半角括号、中文/阿拉伯数字、以及 OCR 常把括号认成方括号的情况。"""
    import re
    t = t or ""
    # (36) / （36） / [36] / 【36】 / (１２) / (3人) / 「36」
    for pat in (r"[（(\[【「]\s*\d{1,5}\s*(?:人)?\s*[）)\]】」]",
                r"[（(]\s*[零一二三四五六七八九十两百]{1,4}\s*[）)]"):
        if re.search(pat, t):
            return True
    return False


def chat_has_sender_names(img):
    """聊天区是否出现「发言人昵称」——微信群里别人发言时，气泡上方会
    单独显示他的昵称；单聊没有这行（见 diag_now.png）。
    判据：左侧（对方气泡区）存在成对的文字块，上一行短、下一行较长，
    且上一行文字不属于时间/系统提示。作为群聊的第二重信号。"""
    try:
        w, h = img.size
        _, _, cx, th = geo(img)
        y_bot = h - INPUT_H - 10
        y_top = max(TITLE_BOT, y_bot - 330)
        chat = img.crop((cx, y_top, w, y_bot))
        cw, ch = chat.size
        blocks = []
        for b in ocr_img(chat):
            t = b["text"].strip()
            if not t or any(k in t for k in UI_WORDS) \
                    or _is_time_text(t) or any(k in t for k in SYS_HINTS):
                continue
            if b["x1"] > cw * 0.65:      # 只看左半（对方消息区），排除我方
                continue
            if _is_self_text(t):
                continue
            blocks.append(b)
        blocks.sort(key=lambda b: b["y1"])
        # 昵称行：短（<=12字）、位于某条较长文字块正上方 60px 内、且 y 间距很小
        for i, b in enumerate(blocks[:-1]):
            nxt = blocks[i + 1]
            if len(b["text"]) <= 12 and len(nxt["text"]) > len(b["text"]) \
                    and 0 < nxt["y1"] - b["y1"] <= 60 \
                    and abs(b["x1"] - nxt["x1"]) < 80:
                # 时间块已经在上面滤掉，这里的基本可认为是「昵称 + 消息」
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _similar(a, b):
    """判断两段 OCR 文本是不是同一个东西（容忍 OCR 错字）"""
    import re
    a = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", a or "")
    b = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", b or "")
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    n = min(4, len(a))
    if any(a[i:i + n] in b for i in range(0, max(1, len(a) - n + 1))):
        return True
    # 字符重合度兜底：OCR 把「费」认成「赛」这种也要算同一个人
    common = sum(min(a.count(c), b.count(c)) for c in set(a))
    return common / max(len(a), len(b)) >= 0.6


def verify_sent(win, text):
    """发完抓一次聊天区，确认最后一条真的是我们发的。
    两条判据任一成立就算成功：
      1) 最后一条文字和我发的内容高度相似（容忍 OCR 错字）
      2) 文字认不准时，若最后一条确实是绿色气泡（自己发的）也算过
    修复：原第 2 条写成 owner == "self" and _similar(text, last)，
    但能走到这里说明 _similar 已是 False，整句恒为 False，
    导致「发出去了但没认出来」时误判失败、再发一遍（重复消息）。"""
    try:
        img, _ = vk.grab(win)
        owner, last = last_message_owner(img)
        if _similar(text, last):
            return True, last
        if owner == "self" and last:
            # 文字没认出来，但气泡归属明确是自己 → 当成已发送
            return True, last
        return False, last
    except Exception as e:  # noqa: BLE001
        log.warning("  校验失败：%s", e)
        return False, None


def _leave_focus(win, prev):
    """发完之后怎么处理前台窗口。
    KEEP_FOREGROUND=True 就一直把微信留在最前面，用户随时能看到回复"""
    if KEEP_FOREGROUND:
        vk.bring_front(win)
    else:
        vk.restore_focus(prev)


def _wait_user_pause(win, need=1.0, limit=3.5):
    """发送前先等你「停手」：你正在敲键盘/动鼠标时，抢过前台会让你的按键打进微信。
    这里最多等 limit 秒，等到有 need 秒没输入动作再发；等不到也照发（消息不能一直压着）"""
    t0 = time.time()
    while time.time() - t0 < limit:
        if vk.user_idle_seconds() >= need:
            return True
        time.sleep(0.25)
    log.info("  你一直在操作，等了 %.1fs 还是先发出去", limit)
    return False


def reply_now(win, w, h, text, dry_run, expect=None, row_y=None, verified=False,
              allow_group=False):
    """真发送需要键盘输入，必须把微信提到前台。
    expect 用来二次确认「现在打开的就是要回的那个会话」，防止切错窗口发错人。
    verified=True 表示刚才点开后刚读过标题、已经确认过，可跳过重复校验。
    allow_group=False 时，发送前最后再抓一次图复核群聊，
    这是防止消息发进群里的最后一道关卡。"""
    prev = vk.foreground_hwnd()
    ix, iy = int(w * INPUT_XR), h - 160
    if dry_run:
        vk.activate(win)
        time.sleep(0.3)
        # 演练也过一遍群聊关卡，才能测出真实会不会发进群
        if not allow_group:
            try:
                gimg, _ = vk.grab(win)
                gtitle = chat_title(gimg)
                if looks_group_title(gtitle) or is_group_name(expect or ""):
                    log.warning("  [演练] 判定为群聊（%s），实际会拦截不发",
                                (expect or gtitle)[:16])
                    _leave_focus(win, prev)
                    return False
            except Exception:  # noqa: BLE001
                pass
        vk.click_in_window(win, ix, iy)
        time.sleep(0.4)
        log.info("  [演练] 将发送：%s", text)
        _leave_focus(win, prev)
        return True

    # ===== 发送前最后一道群聊关卡 =====
    # 无论从「未读红点」还是「当前会话/预览文字」哪条路径进来，
    # 这里都重新抓一次当前画面独立判断，前面漏判的在这里拦下。
    if not allow_group:
        try:
            guard_img, _ = vk.grab(win)
            guard_title = chat_title(guard_img)
            # 强判据：任一成立就拦
            strong = []
            if looks_group_title(guard_title):
                strong.append("标题带人数")
            if is_group_name(expect or "") or is_group_name(guard_title):
                strong.append("名字含群聊关键词")
            # 弱判据：发言人昵称单独可能误报（单聊连续气泡被 OCR 拆块时），
            # 只作参考，不单独触发拦截
            weak = chat_has_sender_names(guard_img)
            if strong:
                log.error("  【防群关卡】判定「%s」是群聊（%s），坚决不发",
                          (expect or guard_title)[:16], "、".join(strong))
                _leave_focus(win, prev)
                return False
            if weak:
                log.warning("  【防群关卡】「%s」疑似群聊（看到发言人昵称），"
                            "谨慎起见不发", (expect or guard_title)[:16])
                _leave_focus(win, prev)
                return False
        except Exception as e:  # noqa: BLE001
            # 关卡自身出错时保守处理：认不出就不发，宁可漏回也不发进群
            log.error("  【防群关卡】复核失败，为安全起见本次不发：%s", e)
            _leave_focus(win, prev)
            return False

    _wait_user_pause(win)
    for attempt in (1, 2):
        vk.activate(win)
        time.sleep(0.35)
        if expect and not verified:
            try:
                cur, _ = vk.grab(win)
                title = chat_title(cur)
            except Exception:  # noqa: BLE001
                title = ""
            if not _similar(expect, title):
                log.warning("  当前是「%s」，不是「%s」，重新点开", title[:14], expect[:14])
                try:                      # 按名字重新定位，行号可能已经变了
                    rows = list_rows(cur)
                    hits = [ny for nm, ny in rows if _similar(expect, nm)]
                    if hits:
                        row_y = hits[0]
                except Exception:  # noqa: BLE001
                    pass
                if row_y:
                    vk.bg_click(win, int(w * ROW_XR), row_y, restore=False)
                    time.sleep(1.5)
                try:
                    cur, _ = vk.grab(win)
                    title = chat_title(cur)
                except Exception:  # noqa: BLE001
                    title = ""
                if not _similar(expect, title):
                    log.error("  切不到「%s」（现在是「%s」），这次不发",
                              expect[:14], title[:14])
                    _leave_focus(win, prev)
                    return False
        vk.click_in_window(win, ix, iy)
        time.sleep(0.4)
        vk.send_text(text)
        time.sleep(0.3)
        vk.press_enter()
        time.sleep(1.2)
        ok, last = verify_sent(win, text)
        if ok:
            log.info("  已发送（已校验）")
            _leave_focus(win, prev)
            return True
        log.warning("  第 %d 次发送没校验到，最后一条是：%s", attempt, (last or "")[:24])
        time.sleep(0.6)

    _leave_focus(win, prev)
    log.error("  发送失败，已放弃这一条")
    return False


def open_chat(win, w, name, y, tries=2):
    """点开会话，并确认现在打开的就是这个名字的会话。
    列表会随时重排，所以对不上时按名字重新定位再点一次。"""
    for i in range(tries):
        vk.bg_click(win, int(w * ROW_XR), y, restore=not KEEP_FOREGROUND)
        time.sleep(1.5)
        img, _ = vk.grab(win)
        title = chat_title(img)
        if name == "未知" or _similar(name, title):
            return img
        rows = list_rows(img)
        hits = [ny for nm, ny in rows if _similar(name, nm)]
        if not hits:
            log.warning("  列表里找不到「%s」，放弃这一行（当前是「%s」）",
                        name[:14], title[:14])
            return None
        y = hits[0]
        log.warning("  点开的是「%s」，按名字定位到「%s」重试", title[:14], name[:14])
    img, _ = vk.grab(win)
    title = chat_title(img)
    if name != "未知" and not _similar(name, title):
        log.warning("  还是没对上（当前「%s」），跳过", title[:14])
        return None
    return img


def eligible(name, only, allow_group):
    """这个会话名能不能回"""
    if not name or name == "未知":
        return True, ""
    if not allow_group and is_group_name(name):
        return False, "群聊默认不回复"
    if any(c in name for c in EXCLUDE_CHARS):
        return False, "名字带 @（企业微信/服务号）"
    for ex in EXCLUDE:
        if ex in name:
            return False, "在排除名单里"
    if only and not any(k in name for k in only):
        return False, "不在 --only 名单里"
    return True, ""


def dedup_unread(unread):
    """同一行可能被红点角标和多处红色像素重复命中，按行去重"""
    seen, out = set(), []
    for u in unread:
        k = u["y"] // 45
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out


def load_state():
    """已回过开场白的人、上次回复时间、上次看到的对方消息、最后互动时间、
    会话模式、我们自己发过的话（用来防止把自己的话当成新消息再回一遍）"""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return (d.get("first_done") or {}, d.get("last_reply") or {},
                d.get("last_seen") or {}, d.get("last_active") or {},
                d.get("modes") or {}, d.get("sent") or {})
    except Exception:  # noqa: BLE001
        return {}, {}, {}, {}, {}, {}


def save_state(first_done, last_reply, last_seen, last_active=None, modes=None,
               sent=None):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"first_done": first_done, "last_reply": last_reply,
                       "last_seen": last_seen,
                       "last_active": last_active or {},
                       "modes": modes or {},
                       "sent": sent or {}}, f,
                      ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log.warning("状态存盘失败：%s", e)


def remember_sent(sent, key, text, keep=3):
    """记下刚发出去的话（只留最近几条），用于下一轮识别「这是我自己发的」"""
    lst = [t for t in (sent.get(key) or []) if isinstance(t, str)]
    lst.append(text)
    sent[key] = lst[-keep:]


def looks_like_our_reply(text, sent, key):
    """最后这条其实是我们刚发出去的吗（OCR 认残了前缀时的兜底判断）。
    必须整句高度相似才算——绝不能用子串判断：
    我们回过「你好呀」，对方再发「你好」绝不是我们自己发的"""
    if not text or len(text) < 8:
        return False
    import difflib
    t = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
    if len(t) < 6:
        return False
    for s in sent.get(key) or []:
        s2 = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", s or "")
        if s2 and difflib.SequenceMatcher(None, t, s2).ratio() >= 0.7:
            return True
    return False


def append_task(name, text):
    """对方按 1 之后列的事务，单独存一份，方便回头一起看"""
    import datetime
    try:
        try:
            with open(TASK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or []
        except Exception:  # noqa: BLE001
            data = []
        data.append({"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "who": name, "text": (text or "").strip()})
        with open(TASK_FILE, "w", encoding="utf-8") as f:
            json.dump(data[-500:], f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log.warning("事务记录失败：%s", e)


def seed_from_logs(first_done):
    """以前已经回过开场文案的人先记上，免得重启后又发一遍固定文案"""
    import glob
    n = 0
    for p in glob.glob(os.path.join(HERE, "run_*.log")):
        cur = None
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.search(r"「(.+?)」", line)
                    if "最后一条" in line and m:
                        cur = m.group(1)
                    elif "已发送" in line and cur:
                        k = norm_key(cur)
                        if k and k not in first_done:
                            first_done[k] = 1
                            n += 1
                        cur = None
        except Exception:  # noqa: BLE001
            continue
    return n


def special_case(title, key, text, modes):
    """先处理“按1 / 按2 / 正在收集事务”这三种情况，都不走 AI。
    返回 (要发的内容, 说明)：
      内容为 ""     → 没命中，走正常流程
      内容为 None   → 这条只记录、不回复
      内容是文本     → 直接发这段固定的话"""
    bare = re.sub(r"[。.！!～~\s]", "", text or "")
    if bare in ("1", "１", "按1"):
        modes[key] = "task"
        return REPLY_PREFIX + TASK_PROMPT, "按了1→让他列事务"
    if bare in ("2", "２", "按2"):
        modes.pop(key, None)
        return REPLY_PREFIX + "好呀，那就陪你聊两句，你最近怎么样？", "按了2→聊天"
    if modes.get(key) == "task":
        append_task(title, text)
        return None, "事务收集中，已记录、不回复"
    return "", ""


def decide_reply(title, key, fallback, img, first_done, use_ai, msgs=None,
                 force_intro=False, gap_min=0):
    """头一条回固定文案（说明身份和电话），之后按对方说的话现场生成。
    force_intro：对方隔了太久才再开口，重新用开场白把身份说一遍"""

    def _out(text, how):
        """所有出口统一加“慧慧”前缀"""
        if REPLY_PREFIX and not text.startswith(REPLY_PREFIX):
            text = REPLY_PREFIX + text
        return text, how

    if force_intro:
        return _out(fallback, "停了 %.0f 分钟，重新开场" % gap_min)
    if key not in first_done:
        return _out(fallback, "开场固定文案")
    if not use_ai:
        return _out(fallback, "固定文案")
    try:
        if msgs is None:
            msgs = chat_messages(img)
    except Exception as e:  # noqa: BLE001
        log.warning("  读聊天记录失败：%s", e)
        return _out(fallback, "读不到记录→固定文案")
    theirs = [m["text"] for m in msgs if m["owner"] == "other"]
    if not theirs:
        return _out(fallback, "没读到对方的话→固定文案")
    log.info("  对方最近说的：%s", " / ".join(theirs[-3:])[:80])
    try:
        r = ai_reply.generate(title, theirs)
    except Exception as e:  # noqa: BLE001
        # AI 模块里任何异常都不能打断整轮扫描，否则这一轮后面的消息全漏掉
        log.warning("  AI 生成出错，改用固定文案：%s", e)
        r = None
    if r:
        return _out(r, "AI生成")
    return _out(fallback, "AI没生成出来→固定文案")


def input_has_text(img):
    """输入框里有没有正在编辑的文字。有人打着字就别插嘴。
    用像素亮度判断（文字是亮色、背景深色），比 OCR 快得多也稳定"""
    try:
        w, h = img.size
        box = img.crop((geo(img)[2] + 30, h - INPUT_H + 10,
                        int(w * 0.96), h - 100))
        small = box.convert("L").resize((box.width // 4 or 1,
                                        box.height // 4 or 1))
        vals = sorted(small.getdata())
        if not vals:
            return False
        med = vals[len(vals) // 2]
        bright = sum(1 for v in vals if v > med + 60)
        return bright > len(vals) * 0.004
    except Exception:  # noqa: BLE001
        return False


def wait_chat(win, tries=5):
    """点完会话后等聊天区渲染出来，返回 (图像, 标题)"""
    img, title = None, ""
    for i in range(tries):
        time.sleep(0.45 if i else 0.6)
        img, _ = vk.grab(win)
        title = chat_title(img)
        if title:
            break
    return img, title


def scan_round(reply_text, dry_run, last_reply, only=None, allow_group=False,
               cooldown=None, last_seen=None, first_done=None, use_ai=True,
               idle_guard=None, last_active=None, reset_min=None, modes=None,
               sent=None):
    """一轮扫描。

    多个人同时发消息时：微信列表最新的在最上面，所以「最下面那条未读」
    就是最早发来的消息，先回它；回完重新截图再取下一个，这样既保证顺序，
    也不会因为列表重排而点到别人头上。

    会话身份只认打开后的聊天标题（标题最准），不依赖列表里 OCR 出来的名字。
    """
    cooldown = COOLDOWN if cooldown is None else cooldown
    idle_guard = IDLE_GUARD if idle_guard is None else idle_guard
    reset_min = RESET_MIN if reset_min is None else reset_min
    last_active = {} if last_active is None else last_active
    modes = {} if modes is None else modes
    last_seen = {} if last_seen is None else last_seen
    first_done = {} if first_done is None else first_done
    sent = {} if sent is None else sent
    only = only or []
    try:
        win = vk.find_wechat()
    except SystemExit:
        log.warning("没找到微信主窗口，等下一轮（别最小化到托盘）")
        return 0
    vk.ensure_onscreen(win)
    if KEEP_FOREGROUND:
        vk.bring_front(win)      # 窗口一直在最前面，回复过程用户都看得见

    done = 0
    done_titles = set()        # 本轮处理过的会话
    repeat = 0                 # 连续点到同一个会话的次数

    for _ in range(12):
        img, _ = vk.grab(win)
        w, h = img.size
        unread = dedup_unread(find_unread(img, with_names=False))
        if not unread:
            # 预览文字检测默认关闭：它和本函数末尾的「当前会话检查」功能重复，
            # 而且容易把自己刚发的回复当成新消息，导致反复点开同一行刷屏。
            # 真需要时（比如红点识别长期失灵）把 ENABLE_PREVIEW_DETECT 设 True。
            if ENABLE_PREVIEW_DETECT:
                try:
                    suspects = preview_suspects(img)
                except Exception as e:  # noqa: BLE001
                    log.debug("预览文字检测失败：%s", e)
                    suspects = []
                for s in suspects:
                    unread.append({"y": s["y"], "x": 0, "count": 1,
                                   "name": s["name"], "from_preview": True})
                unread = dedup_unread(unread)
                if suspects:
                    log.info("预览文字发现 %d 个无红点的新消息", len(suspects))
        if not unread:
            break

        u = max(unread, key=lambda x: x["y"])     # 最下面 = 最早发的
        # 立刻点，中间不跑 OCR，否则列表早重排了
        vk.bg_click(win, int(w * ROW_XR), u["y"], restore=not KEEP_FOREGROUND)
        img2, title = wait_chat(win, tries=3)
        if not title:
            # 抓到了切换动画的空白帧：原位置补点一次，比重新扫列表快得多
            log.warning("点开 y=%d 没读到标题，原位置再点一次", u["y"])
            vk.bg_click(win, int(w * ROW_XR), u["y"], restore=not KEEP_FOREGROUND)
            img2, title = wait_chat(win, tries=3)
        if not title:
            log.warning("点开 y=%d 仍然没读到标题，跳过", u["y"])
            continue

        key = norm_key(title)
        if key in done_titles:
            repeat += 1
            if repeat >= 2:
                log.info("连续两次点开都是「%s」，本轮先停，下一轮再试", title[:14])
                break
            log.info("「%s」本轮已处理过，跳过", title[:14])
            continue
        repeat = 0
        done_titles.add(key)

        ok, why = eligible(title, only, allow_group)
        if not ok:
            log.info("「%s」%s，跳过", title[:14], why)
            continue
        if not allow_group and (looks_group_title(title)
                                or is_group_name(u.get("name") or "")
                                or is_group_name(title)):
            # 双保险：标题认不出人数时，列表行名字带群聊关键词也拦下，
            # 绝不能把开场白发到群里去
            log.info("「%s」判定为群聊，跳过", title[:14])
            continue
        if time.time() - last_reply.get(key, 0) < max(cooldown, MIN_GAP):
            log.info("「%s」冷却中，跳过", title[:14])
            continue

        # 直接用气泡归属结果，比“绿色最低位置”那套准，也省一次 OCR
        msgs = chat_messages(img2)
        if msgs:
            owner, text = msgs[-1]["owner"], msgs[-1]["text"]
        else:
            owner, text = last_message_owner(img2)
        log.info("「%s」最后一条（%s）：%s（剩余未读 %d）",
                 title[:14], owner, (text or "")[:30], max(0, len(unread) - 1))
        if owner != "other":
            last_seen[key] = text
            continue
        if looks_like_our_reply(text, sent, key):
            # 兜底：绿色气泡没认出来、前缀又被 OCR 认残时，
            # 靠“跟我刚发的那句几乎一样”认出来，绝不能回自己
            log.info("  这条是我自己刚发的，不回")
            last_seen[key] = text
            continue
        if any(k in (text or "") for k in MSG_SKIP):
            log.info("  内容像广告/群发，不回")
            last_seen[key] = text
            continue
        if last_seen.get(key) == text or _similar(last_seen.get(key), text):
            # 能走到这里说明列表里真的有未读角标 = 对方发了新消息，
            # 就算文字和上次一模一样（比如又发了句「速来」）也该回；
            # 只有刚回过没多久才真正跳过，防止角标残留导致反复回旧消息
            if time.time() - last_reply.get(key, 0) < 120:
                log.info("  这条已经回过了，跳过")
                continue

        # 按 1 / 按 2 / 正在收集事务，这三种都不走 AI
        sp_text, sp_how = special_case(title, key, text, modes)
        if sp_text is None:
            log.info("  %s", sp_how)
            last_seen[key] = text
            last_active[key] = time.time()
            save_state(first_done, last_reply, last_seen, last_active, modes, sent)
            continue

        # 对方隔太久才再开口，就把开场白重新说一遍（介绍身份和电话）
        prev_ts = last_active.get(key, 0)
        gap_min = (time.time() - prev_ts) / 60.0 if prev_ts else 0.0
        force_intro = bool(reset_min and prev_ts) and gap_min >= reset_min
        last_active[key] = time.time()
        if force_intro:
            modes.pop(key, None)          # 隔太久，之前“列事务”的状态作废

        if sp_text:
            out_text, how = sp_text, sp_how
        else:
            out_text, how = decide_reply(title, key, reply_text, img2,
                                         first_done, use_ai, msgs,
                                         force_intro, gap_min)
        ok_rate, why_rate = reply_rate_ok(key)
        if not ok_rate:
            log.info("  %s，这次跳过", why_rate)
            continue
        log.info("  准备回复（%s）：%s", how, out_text[:40])
        if reply_now(win, w, h, out_text, dry_run, expect=title, verified=True,
                     allow_group=allow_group):
            last_reply[key] = time.time()
            last_seen[key] = text
            first_done[key] = 1
            remember_sent(sent, key, out_text)
            _REPLY_LOG.append((time.time(), key))
            done += 1
            save_state(first_done, last_reply, last_seen, last_active, modes, sent)
        time.sleep(0.7)

    # 正在打开的那个会话收新消息时不会出现红点，单独查一次。
    # 但人还在电脑前打字/动鼠标时绝不抢话——那是本人正在聊的会话。
    try:
        idle = vk.user_idle_seconds()
        img, _ = vk.grab(win)
        # 唯一会让开的理由：输入框里有你正在打的字。
        # 不再看鼠标动不动，所以你不用为了让它回消息特意把鼠标挪开
        if input_has_text(img):
            log.info("输入框里有字（你在打字），先不插嘴")
            return done
        if idle_guard and idle < idle_guard:
            return done
        w, h = img.size
        title = chat_title(img)
        if title and eligible(title, only, allow_group)[0] \
                and not (not allow_group and
                         (looks_group_title(title) or is_group_name(title))):
            key = norm_key(title)
            if time.time() - last_reply.get(key, 0) >= max(cooldown, CUR_CHAT_GAP):
                msgs = chat_messages(img)
                if msgs:
                    owner, text = msgs[-1]["owner"], msgs[-1]["text"]
                else:
                    owner, text = last_message_owner(img)
                if owner == "other" and text \
                        and not any(k in text for k in MSG_SKIP) \
                        and not looks_like_our_reply(text, sent, key):
                    # 和上次见过的文字一样不算事：只要不是刚回过（2 分钟内），
                    # 对方重复发同一句（比如又问一遍「你是机器人吗」）也要回
                    seen_same = (last_seen.get(key) == text
                                 or _similar(last_seen.get(key), text))
                    if seen_same and time.time() - last_reply.get(key, 0) < 120:
                        log.info("  「%s」刚回过这句，跳过", title[:12])
                        return done
                    log.info("当前会话「%s」收到新消息：%s", title[:16], text[:24])
                    sp_text, sp_how = special_case(title, key, text, modes)
                    if sp_text is None:
                        log.info("  %s", sp_how)
                        last_seen[key] = text
                        last_active[key] = time.time()
                        save_state(first_done, last_reply, last_seen,
                                   last_active, modes, sent)
                        return done
                    gap_min = ((time.time() - last_active[key]) / 60.0
                               if last_active.get(key) else 0.0)
                    force_intro = bool(reset_min and last_active.get(key)) \
                        and gap_min >= reset_min
                    last_active[key] = time.time()
                    if force_intro:
                        modes.pop(key, None)
                    if sp_text:
                        out_text, how = sp_text, sp_how
                    else:
                        out_text, how = decide_reply(title, key, reply_text, img,
                                                     first_done, use_ai, msgs,
                                                     force_intro, gap_min)
                    ok_rate, why_rate = reply_rate_ok(key)
                    if not ok_rate:
                        log.info("  %s，这次跳过", why_rate)
                        return done
                    log.info("  准备回复（%s）：%s", how, out_text[:40])
                    if reply_now(win, w, h, out_text, dry_run, expect=title, verified=True,
                                 allow_group=allow_group):
                        last_reply[key] = time.time()
                        last_seen[key] = text
                        first_done[key] = 1
                        remember_sent(sent, key, out_text)
                        _REPLY_LOG.append((time.time(), key))
                        done += 1
                        save_state(first_done, last_reply, last_seen,
                                   last_active, modes, sent)
    except Exception as e:  # noqa: BLE001
        log.error("检查当前会话出错：%s", e)

    return done


def acquire_single_instance():
    """同一时间只允许一个实例在跑。之前出现过两个进程同时回同一条消息"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        handle = k32.CreateMutexW(None, False, "Local\\HuiHuiWechatAutoReply")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:  # noqa: BLE001
        return True


def main():
    p = argparse.ArgumentParser(description="微信4.x 图像识别自动回复")
    p.add_argument("--reply", default=DEFAULT_REPLY, help="回复文案")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL)
    p.add_argument("--cooldown", type=int, default=COOLDOWN)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--only", default="", help="只回这些会话，逗号分隔（名字含关键词即可）")
    p.add_argument("--allow-group", action="store_true", help="允许回复群聊（默认一律不回）")
    p.add_argument("--no-ai", action="store_true",
                   help="只发固定文案，不做内容生成（默认是第一条固定、之后按内容回）")
    p.add_argument("--reset-minutes", type=int, default=RESET_MIN,
                   help="对方隔多少分钟才再开口就重新回开场白（默认 30，0 = 关闭）")
    p.add_argument("--idle-guard", type=int, default=IDLE_GUARD,
                   help="超过多少秒没动键鼠就不接管，0 = 不看鼠标（默认 0）")
    args = p.parse_args()

    lock = acquire_single_instance()
    if lock is None:
        log.error("已经有一个慧慧在跑了，这次启动直接退出（避免两个进程抢着回复）")
        return

    log.info("连接微信…")
    try:
        win = vk.find_wechat()
        # 启动时统一成标准大小，保证布局可预期；运行中拖大也没问题
        vk.ensure_onscreen(win, normalize=True)
        log.info("窗口已就位：%s @ %s", win["title"], win["rect"])
    except SystemExit:
        log.warning("当前没找到微信主窗口，启动后会一直等它出现")

    only = [k.strip() for k in args.only.split(",") if k.strip()]
    fresh = not os.path.exists(STATE_PATH)
    first_done, last_reply, last_seen, last_active, modes, sent = load_state()
    # 只在第一次运行（还没有状态文件）时，从旧日志里认领历史回复过的人，
    # 否则会把你手动清空的名单又补回来
    seeded = seed_from_logs(first_done) if fresh else 0
    if seeded:
        log.info("首次运行，按历史日志认领以前回过的人 %d 个", seeded)
    total = 0

    # 启动时先记住当前打开的那个会话，避免把之前的旧消息当成新消息回一遍
    try:
        img0, _ = vk.grab(vk.find_wechat())
        t0 = chat_title(img0)
        if t0:
            _, tx0 = last_message_owner(img0)
            last_seen[norm_key(t0)] = tx0
            log.info("当前打开的是「%s」，已记下，不当成新消息", t0[:14])
        # 预览文字检测的首轮基线也在启动时种下：
        # 否则第一次扫描会把「启动前就存在的预览」当成新消息误报
        preview_suspects(img0)
    except Exception:  # noqa: BLE001
        pass

    if args.no_ai:
        log.info("回复模式：只发固定文案")
    elif ai_reply.has_llm():
        log.info("回复模式：第一条固定文案，之后按内容生成（%s）",
                 ai_reply.load_config().get("model"))
    else:
        log.warning("回复模式：第一条固定文案，之后按本地话术回"
                    "（想让它自由组织语言，去 wx_ai_config.json 填 api_key）")

    log.info("开始监听 | 文案：%s | 间隔 %.0fs | Ctrl+C 退出",
             args.reply, args.interval)

    while True:
        try:
            n = scan_round(args.reply, args.dry_run, last_reply, only,
                           args.allow_group, args.cooldown, last_seen,
                           first_done, not args.no_ai, args.idle_guard,
                           last_active, args.reset_minutes, modes, sent)
            total += n
            if n:
                log.info("本轮回复 %d（累计 %d）", n, total)
        except Exception as e:  # noqa: BLE001
            log.error("本轮出错：%s", e)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("已停止")
