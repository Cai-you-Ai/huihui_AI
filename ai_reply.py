# -*- coding: utf-8 -*-
"""
AI 回复生成模块

两种模式：
  1. 大模型模式：在 wx_ai_config.json 里填 api_key（OpenAI 兼容接口均可，
     DeepSeek / 通义 / Kimi / 智谱 / OpenAI 都能用），由模型按人设生成一句话回复
  2. 规则模式：没配 Key 时用本地话术库兜底，能应付常见问句，聊不来就回固定文案

对外只有一个函数：
  generate(name, texts) -> str | None
    name  对方微信昵称
    texts 对方最近说的几句（按时间顺序）
    返回要发出去的那句话；返回 None 表示没有办法生成，调用方用固定文案兜底
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _say(msg):
    """pythonw 运行时没有控制台，print 会抛异常；有就写、没有就静默。"""
    try:
        if sys.stderr is not None:
            sys.stderr.write(str(msg) + "\n")
    except Exception:  # noqa: BLE001
        pass
CONFIG_PATH = os.path.join(HERE, "wx_ai_config.json")

PHONE = "19113350934"

DEFAULT_PERSONA = (
    "你是朱恩锐的 AI 助手，名叫慧慧，正在用他本人的微信替他回消息。"
    "说话像真人在微信打字：短、口语、一句话说完，20 到 45 个字，"
    "不用书面语，不加引号，不用表情符号，不分条，不解释你在做什么。"
    "\n先判断对方要干什么，再决定怎么回："
    "\n· 想聊天、打招呼、寒暄、开玩笑 → 先顺着他接一句、把话题接住，"
    "聊得下去就聊，别每一句都用“等他回来”打发人；"
    "\n· 问事情、问进度、要东西 → 你知道就说知道，不知道就说等他回来确认，绝不替他猜；"
    "\n· 问价格、时间、见面、借钱、要承诺 → 一律说不清楚，等他回来跟他本人谈；"
    "\n· 有急事、催 → 让他直接打 19113350934；"
    "\n· 对方不高兴、抱怨被怠慢 → 先道个歉安抚一句，再说他会尽快回；"
    "\n· 对方问对他自己的评价（帅吗、好看吗、你觉得呢）→ 用轻松的话挡回去，别硬答也别跑题。"
    "\n要紧的是：对方问什么就答什么，别绕开他最新那句去说别的。"
    "\n绝对不做：承诺金额、时间、合同、见面；透露他的行程住址和隐私；"
    "替朱恩锐做决定；假装自己是朱恩锐本人；说“不卖”“没有”这类生硬的话。"
    "\n每条回复至少 15 个字，别只丢一句“等他回来确认”就完事，要把话说完整、说软一点。"
    "\n参考语气："
    "\n对方：可以和我聊聊天吗　→　可以呀，不过他这会儿真不在，我先陪你聊两句，你想聊啥"
    "\n对方：你是谁啊　→　我是他的助手慧慧，他不在的时候我帮他盯着消息~"
    "\n对方：在干嘛呢　→　我在这儿帮他回消息呢，他人在外面忙~"
    "\n对方：这个多少钱　→　这个数我不清楚，等他回来我让他马上跟你确认~"
    "\n对方：明天下午开会吗　→　时间还得他本人定，等他回来我让他第一时间联系你~"
    "\n对方：怎么不回我　→　不好意思啊，他不在，我看到就先替他回你一句~"
    "\n对方：你觉得我帅吗　→　这个我可不敢乱评价，等他回来让他亲自说~"
)

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "persona": "",
    "max_chars": 45,
    "context_count": 3,
    "timeout": 15,
}

# 生成结果里出现这些词，一律作废（防止模型乱承诺、套隐私）
BANNED = ("转账", "付款码", "银行卡", "密码", "身份证", "验证码",
          "我马上到", "我马上来", "我一定", "保证", "合同", "签字",
          "多少钱我给你", "借你", "打给你")

# 没配 Key 时的本地话术（按顺序匹配，命中第一条就走）
RULES = [
    (("急", "赶紧", "马上", "立刻", "等着", "催"),
     "他这会儿不在，急事直接打他电话 19113350934 最快~"),
    (("在吗", "在不在", "在么", "你好", "您好", "hi", "hello", "哈喽", "喂"),
     "在的，我是慧慧，朱恩锐的AI助手，他暂时不在~"),
    (("多少钱", "价格", "报价", "费用", "预算", "转账", "付款", "发票"),
     "这个数我不清楚，等他回来我让他马上跟你确认~"),
    (("什么时候", "几点", "时间", "约", "见面", "开会", "安排"),
     "时间上我做不了主，等他回来我让他第一时间跟你定~"),
    (("在哪", "位置", "地址", "公司", "办公室"),
     "他人在外面，具体位置我不方便说，有急事打电话 19113350934~"),
    (("谢谢", "多谢", "感谢", "辛苦", "麻烦了"),
     "不客气哈，他回来我让他联系你~"),
    (("好嘞", "收到", "知道了", "明白", "可以的"),
     "好嘞，我记下了，他回来我跟他说~"),
    (("聊聊天", "聊两句", "聊聊", "说说话", "陪我", "聊会"),
     "好呀，不过他这会儿真不在，我先陪你聊两句~"),
    (("你是谁", "机器人", "AI", "自动回复", "助手"),
     "我是他的AI助手慧慧，他现在不在，有事我可以先帮你记着~"),
    (("吃饭", "睡了", "忙", "累", "休息"),
     "哈哈他最近确实挺忙的，等他空了我让他找你~"),
    (("?", "？", "怎么回事", "什么情况", "真的假的"),
     "这个我这边不太清楚，等他回来让他亲自跟你说~"),
]

# 一句都匹配不上时的通用过渡语（比重复发开场白自然）
DEFAULT_FALLBACK = "嗯嗯，我记下了，等他回来第一时间让他联系你~"

_cache = {"cfg": None, "mtime": 0}


def load_config():
    """读配置，文件改了会自动重新加载"""
    try:
        mt = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return dict(DEFAULT_CONFIG)
    if _cache["cfg"] is not None and _cache["mtime"] == mt:
        return _cache["cfg"]
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception as e:  # noqa: BLE001
        _say("[ai] 配置文件读不出来（%s），先用默认值" % e)
    _cache["cfg"], _cache["mtime"] = cfg, mt
    return cfg


def has_llm():
    cfg = load_config()
    return bool(str(cfg.get("api_key") or "").strip())


def _clean(text, max_chars):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^(回复|答复)[:：]\s*", "", t)
    t = t.strip().strip('"').strip("'").strip("“”「」《》")
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    if not t:
        return None
    if len(t) > max_chars:
        cut = t[:max_chars]
        for sep in ("。", "！", "？", "，", "~", " "):
            i = cut.rfind(sep)
            if i >= max_chars * 0.5:
                cut = cut[:i + 1]
                break
        t = cut
    for w in BANNED:
        if w in t:
            return None
    return t


def _chat(cfg, messages):
    url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 120,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(cfg["api_key"]).strip(),
    })
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 15))) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def generate(name, texts):
    """返回要发出去的回复；None 表示生成失败，调用方兜底"""
    texts = [t.strip() for t in (texts or []) if t and t.strip()]
    if not texts:
        return None
    cfg = load_config()
    max_chars = int(cfg.get("max_chars", 45))
    cfg["context_count"] = int(cfg.get("context_count", 3))
    ctx_texts = texts[-cfg["context_count"]:]
    last = ctx_texts[-1]

    # 开场白里说了“有事请按1，想聊天请按2”，这里得真的接住这两个字
    if last.strip() in ("1", "１"):
        return _clean("好，你说，我记下来转告他", max_chars)
    if last.strip() in ("2", "２"):
        return _clean("好呀，那就陪你聊两句，你最近怎么样？", max_chars)

    if has_llm():
        persona = (cfg.get("persona") or "").strip() or DEFAULT_PERSONA
        sys_prompt = persona + "\n对方微信昵称：" + name
        last_msg = ctx_texts[-1]
        prev = ctx_texts[:-1]
        user_prompt = "对方最新发来的一句（重点回这一句，别答非所问）：「%s」\n" % last_msg
        if prev:
            user_prompt += ("前面几句只是背景，不要重复回应它们：\n"
                            + "\n".join("· " + t for t in prev) + "\n")
        user_prompt += "\n现在写你要回他的那一句话，只输出这句话本身。"
        try:
            raw = _chat(cfg, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ])
            out = _clean(raw, max_chars)
            if out:
                return out
        except urllib.error.HTTPError as e:
            _say("[ai] 模型返回错误 %s：%s" % (e.code, e.read()[:200]))
        except Exception as e:  # noqa: BLE001
            _say("[ai] 调用模型失败：%s" % e)

    # 没有 Key 或调用失败 → 本地话术
    # 只按“新到旧”逐条判断：不能把几句拼起来匹配，否则历史里的“你好”
    # 会永远盖住他最新说的那句话，回复就卡死在同一句上
    for t in reversed(ctx_texts):
        probe = t.lower()
        for keys, reply in RULES:
            if any(k.lower() in probe for k in keys):
                return _clean(reply, max_chars)
    return _clean(DEFAULT_FALLBACK, max_chars)


if __name__ == "__main__":
    import sys

    cfg = load_config()
    print("接口：%s" % cfg["base_url"])
    print("模型：%s" % cfg["model"])
    print("Key ：%s" % ("已配置" if has_llm() else "未配置（现在走本地话术）"))
    print("-" * 40)
    for t in (sys.argv[1:] or ["你在吗", "这个多少钱", "可以和我聊聊天吗"]):
        print("%s  ->  %s" % (t[:18], generate("测试", [t])))
