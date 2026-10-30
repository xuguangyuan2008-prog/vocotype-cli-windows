"""录音状态提示：屏幕底部中间一个小浮条，加提示音。

按住 F2 开始录音：响一声，浮条「● 正在录音」；松开 F2：换一声，浮条变「识别中…」；文字打出来后浮条消失。
浮条不抢焦点（不然识别出的字会打到浮条上），鼠标也点不到它。Tk 自己跑在一个线程里，别的线程只往队列里放状态。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading

logger = logging.getLogger(__name__)

_STYLES = {
    "recording": ("●  正在录音   松开 F2 结束", "#d6453d"),
    "busy": ("识别中…", "#c98a1e"),
}


class Indicator:
    def __init__(self, sound: bool = True) -> None:
        self._sound = sound
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._run, name="indicator", daemon=True).start()

    # ------------------------------------------------------------ 给别的线程用

    def recording(self) -> None:
        self._beep(880, 120)
        self._q.put("recording")

    def preview(self, text: str) -> None:
        """录音中边说边显示识别出的字。"""
        self._q.put("preview:" + text)

    def busy(self) -> None:
        self._beep(587, 120)
        self._q.put("busy")

    def hide(self) -> None:
        self._q.put("hide")

    def _beep(self, freq: int, ms: int) -> None:
        if not self._sound or sys.platform != "win32":
            return
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()

    # ------------------------------------------------------------ Tk 线程

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:  # 没有 Tk 就只有提示音
            logger.warning("录音浮条不可用: %s", exc)
            return
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        # 显示 / 隐藏只改透明度，不再 deiconify / withdraw：那两个会把焦点抢过来
        root.attributes("-alpha", 0.0)
        label = tk.Label(
            root, font=("Microsoft YaHei UI", 12, "bold"), fg="white", padx=18, pady=7,
            justify="left", wraplength=int(root.winfo_screenwidth() * 0.5),
        )
        label.pack()
        self._no_focus(root)
        root.geometry("1x1+-100+-100")
        root.deiconify()

        def show(state: str, text: str | None = None) -> None:
            base, color = _STYLES[state]
            text = text or base
            label.config(text=text, bg=color)
            root.config(bg=color)
            root.update_idletasks()
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            x = (root.winfo_screenwidth() - w) // 2
            y = root.winfo_screenheight() - h - 90
            root.geometry(f"{w}x{h}+{x}+{y}")
            root.attributes("-alpha", 0.92)
            root.attributes("-topmost", True)

        shown = {"state": "hide", "n": 0}

        def hide() -> None:
            root.attributes("-alpha", 0.0)
            root.geometry("1x1+-100+-100")

        def give_up(n: int) -> None:
            # 「识别中」等太久（没说话、出错都可能没有结果回来）就自己收起
            if shown["state"] == "busy" and shown["n"] == n:
                hide()
                shown["state"] = "hide"

        def poll() -> None:
            try:
                while True:
                    state = self._q.get_nowait()
                    if state.startswith("preview:"):
                        # 只在录音状态下更新；松开后才到的旧预览不要
                        if shown["state"] == "recording":
                            words = state[len("preview:"):]
                            if len(words) > 80:
                                words = "…" + words[-80:]
                            show("recording", "●  " + words)
                        continue
                    shown["state"] = state
                    shown["n"] += 1
                    if state == "hide":
                        hide()
                    else:
                        show(state)
                        if state == "busy":
                            root.after(15000, give_up, shown["n"])
            except queue.Empty:
                pass
            root.after(50, poll)

        poll()
        root.mainloop()

    @staticmethod
    def _no_focus(root) -> None:
        """Windows：不抢焦点、不进任务栏、鼠标穿透。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
            WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception as exc:
            logger.debug("设置浮条不抢焦点失败: %s", exc)
