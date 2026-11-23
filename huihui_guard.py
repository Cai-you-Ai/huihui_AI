# -*- coding: utf-8 -*-
"""慧慧看门狗：盯着主程序，崩了就自动拉起来。

启动慧慧.bat / 开机自启跑的都是这个，
所以就算 OCR 引擎偶尔崩溃，最多断几秒就会恢复监听。
"""
import ctypes
import datetime
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "wechat_vision_auto_reply.py")
LOG = os.path.join(HERE, "logs", "guard.log")
ARGS = ["--interval", "2", "--cooldown", "8", "--reset-minutes", "30"]


def acquire_mutex(name):
    """全局只允许一个看门狗。系统偶尔会把旧任务用别的 Python 拉起来，
    没有这把锁就会出现两套慧慧互相打架"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183      # 183 = 已存在


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


def main():
    if not acquire_mutex("Local\\HuiHuiWechatGuard"):
        log("已有看门狗在跑，本次直接退出")
        return
    # 优先用项目自带的 venv 解释器，系统里别的 python 依赖不一定全
    venv_py = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    py = venv_py if os.path.exists(venv_py) else sys.executable
    log("=== 看门狗启动 (%s) ===" % py)
    while True:
        log("拉起主程序")
        code = None
        try:
            p = subprocess.Popen([py, MAIN] + ARGS, cwd=HERE)
            code = p.wait()
        except Exception as e:  # noqa: BLE001
            log("启动出错：%r" % (e,))
            code = -1
        log("主程序退出 code=%s，5 秒后重启" % code)
        time.sleep(5)


if __name__ == "__main__":
    main()
