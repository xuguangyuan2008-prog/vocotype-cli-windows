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
    "info": ("", "#2f8f5b"),
    "error": ("", "#b23b3b"),
    "loading": ("VocoType 启动中…  正在加载语音模型", "#4a5d78"),
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

    def loading(self) -> None:
        """启动时加载模型期间一直显示，直到下一个状态替换它。"""
        self._q.put("loading")

    def info(self, text: str, seconds: float = 2.5, error: bool = False) -> None:
        """绿色（出错红色）提示条，几秒后自己收起。"""
        self._q.put(("info", text, seconds, error))

    def correct(self, text: str, on_done) -> None:
        """弹出纠错窗口。改好按 Enter → on_done(新文字, True)；Ctrl+Enter 只记住不替换 → on_done(新文字, False)。"""
        self._q.put(("correct", text, on_done))

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
            # 「识别中」等太久（没说话、出错都可能没有结果回来）就自己收起；提示条到时间收起
            if shown["state"] in ("busy", "info") and shown["n"] == n:
                hide()
                shown["state"] = "hide"

        def poll() -> None:
            try:
                while True:
                    state = self._q.get_nowait()
                    if isinstance(state, tuple):
                        if state[0] == "info":
                            shown["state"] = "info"
                            shown["n"] += 1
                            show("error" if len(state) > 3 and state[3] else "info", state[1])
                            root.after(int(state[2] * 1000), give_up, shown["n"])
                        elif state[0] == "correct":
                            self._correct_window(tk, root, state[1], state[2])
                        continue
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

    _open_win = None  # 已经开着的纠错窗口，再按 F3 只把它提到前面

    @classmethod
    def _correct_window(cls, tk, root, text: str, on_done) -> None:
        if cls._open_win is not None and cls._open_win.winfo_exists():
            _force_foreground(cls._open_win)
            return
        win = tk.Toplevel(root)
        cls._open_win = win
        win.title("VocoType 纠错")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        font = ("Microsoft YaHei UI", 12)
        tk.Label(
            win, font=("Microsoft YaHei UI", 10), fg="#555", justify="left",
            text="把识别错的地方改对，程序会记住，下次自动改正。\n"
                 "Enter：记住并替换刚才输入的文字　Ctrl+Enter：只记住　Esc：取消",
        ).pack(anchor="w", padx=14, pady=(12, 6))
        box = tk.Text(win, font=font, width=48, height=4, wrap="char", undo=True)
        box.insert("1.0", text)
        box.pack(padx=14, pady=(0, 14))
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")

        def finish(replace: bool | None):
            new = box.get("1.0", "end-1c").strip()
            win.destroy()
            if replace is not None:
                on_done(new, replace)
            return "break"

        box.bind("<Return>", lambda e: finish(True))
        box.bind("<Control-Return>", lambda e: finish(False))
        box.bind("<Escape>", lambda e: finish(None))
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        _force_foreground(win)
        box.focus_force()
        box.tag_add("sel", "1.0", "end-1c")

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


def _force_foreground(win) -> None:
    """后台程序弹的窗口 Windows 默认不给焦点；先按一下 Alt 再抢前台（常用办法）。"""
    win.update_idletasks()
    if sys.platform != "win32":
        win.focus_force()
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("纠错窗口抢焦点失败: %s", exc)
    win.focus_force()


def foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    return ctypes.windll.user32.GetForegroundWindow()


def restore_foreground(hwnd: int) -> None:
    if not hwnd or sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def press_backspace(n: int) -> None:
    if n <= 0 or sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    for _ in range(n):
        user32.keybd_event(0x08, 0, 0, 0)
        user32.keybd_event(0x08, 0, 2, 0)


def tap_key(vk: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
