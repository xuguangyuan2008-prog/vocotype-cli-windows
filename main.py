"""Command-line entry for the speak-keyboard prototype."""

from __future__ import annotations

import os

# 模型放在项目的 models 文件夹（不占 C 盘）；要放别处就自己设 MODELSCOPE_CACHE
os.environ.setdefault("MODELSCOPE_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))

import argparse
import logging
import queue
import threading
import time

import keyboard

from app import HotkeyManager, TranscriptionResult, TranscriptionWorker, load_config, type_text
from app.plugins.dataset_recorder import wrap_result_handler
from app.logging_config import setup_logging
from app import indicator as ui
from app.indicator import Indicator


logger = logging.getLogger(__name__)


_TOGGLE_DEBOUNCE_SECONDS = 0.2
_toggle_lock = threading.Lock()
_last_toggle_time = 0.0
# 录音提示：浮条 + 提示音（按 F2 原来什么提示都没有）
_indicator: Indicator | None = None
# 最近一次输入的文字（纠错窗口用），以及输入时的前台窗口
_last = {"text": "", "hwnd": 0}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Speak Keyboard prototype")
    parser.add_argument("--config", help="Path to config JSON")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single transcription cycle for debugging",
    )
    parser.add_argument("--save-dataset", action="store_true", help="Persist audio/text pairs")
    parser.add_argument("--dataset-dir", default="dataset", help="Dataset output directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.config:
        # 项目目录下有 config.json 就用它（不用每次带 --config）
        import os
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(default):
            args.config = default
    config = load_config(args.config)
    
    # 配置日志系统（统一配置）
    from app.config import ensure_logging_dir
    log_dir_abs = ensure_logging_dir(config)
    setup_logging(
        level=config["logging"].get("level", "INFO"),
        log_dir=log_dir_abs
    )

    output_cfg = config.get("output", {})
    output_method = output_cfg.get("method", "auto")
    append_newline = output_cfg.get("append_newline", False)

    # 浮条先起来：加载模型要十几秒（刚开机要一两分钟），期间显示「启动中」
    global _indicator
    _indicator = Indicator(sound=config.get("indicator", {}).get("sound", True))
    _indicator.loading()

    # 先创建worker（没有回调）
    try:
        worker = TranscriptionWorker(
            config_path=args.config,
            on_result=None,  # 稍后设置
        )
    except Exception:
        _indicator.info("VocoType 启动失败，详情见 logs 里的日志", 8, error=True)
        time.sleep(8)
        raise
    
    # 创建result handler（需要worker引用）
    worker.on_result = _make_result_handler(output_method, append_newline, worker)
    if args.save_dataset:
        worker.on_result = wrap_result_handler(worker.on_result, worker, args.dataset_dir)
    
    if config.get("preview", {}).get("enabled", True):
        worker.on_preview = _indicator.preview

    hotkeys = HotkeyManager()

    toggle_combo = config["hotkeys"].get("toggle", "f2")
    hold = config["hotkeys"].get("mode", "hold") == "hold"
    if hold:
        _register_hold(toggle_combo, worker)
    else:
        hotkeys.register(toggle_combo, lambda: _toggle(worker))

    correct_key = config["hotkeys"].get("correct", "f3")
    if correct_key:
        # 长按会连发按下事件，只认第一次；同样别在钩子里干活
        f3_down = {"v": False}

        def on_correct(event) -> None:
            if event.event_type == keyboard.KEY_DOWN:
                if not f3_down["v"]:
                    f3_down["v"] = True
                    threading.Thread(target=_open_correction, args=(worker, output_method), daemon=True).start()
            else:
                f3_down["v"] = False

        keyboard.hook_key(correct_key, on_correct, suppress=True)
        logger.info("已注册纠错热键 %s", correct_key)

    try:
        logger.info("Speak Keyboard 启动完成，%s %s 录音，按 Ctrl+C 退出", "按住" if hold else "按", toggle_combo)
        _watch_hook(worker)
        _indicator.info(f"VocoType 可以用了：{'按住' if hold else '按'} {toggle_combo.upper()} 说话", 3)
        if args.once:
            _toggle(worker)
            input("按 Enter 停止并退出...")
            _toggle(worker)
        else:
            keyboard.wait()
    except KeyboardInterrupt:
        logger.info("用户中断，正在退出...")
    finally:
        # 清理所有资源
        try:
            worker.stop()
        except Exception as exc:
            logger.debug("停止 worker 时出错: %s", exc)
        
        try:
            worker.cleanup()
        except Exception as exc:
            logger.debug("清理 worker 时出错: %s", exc)
        
        try:
            hotkeys.cleanup()
        except Exception as exc:
            logger.debug("清理热键时出错: %s", exc)
        
        logger.info("所有资源已清理，正常退出")
        import sys
        sys.exit(0)


def _make_result_handler(output_method: str, append_newline: bool, worker: TranscriptionWorker):
    def _done() -> None:
        # 没在录音、也没有排队的就收起浮条
        if _indicator and not worker.is_running and worker.transcription_stats["pending"] == 0:
            _indicator.hide()

    def _handle_result(result: TranscriptionResult) -> None:
        if result.error:
            logger.error("转写失败: %s", result.error)
            _done()
            return

        # 获取转录统计信息
        stats = worker.transcription_stats
        
        logger.info(
            "转写成功: %s (推理 %.2fs) [已完成 %d/%d，队列剩余 %d]",
            result.text,
            result.inference_latency,
            stats["completed"],
            stats["submitted"],
            stats["pending"],
        )
        if result.text:
            type_text(
                result.text,
                append_newline=append_newline,
                method=output_method,
            )
            _last["text"] = result.text
            _last["hwnd"] = ui.foreground_window()
        _done()

    return _handle_result


def _open_correction(worker: TranscriptionWorker, output_method: str) -> None:
    """F3：弹出最近一句，改对后记住；按 Enter 还会把刚才输入的那句换成改好的。"""
    before = _last["text"]
    if not _indicator:
        return
    if not before:
        _indicator.info("还没有可以纠错的句子")
        return
    hwnd = ui.foreground_window()

    def done(after: str, replace: bool) -> None:
        def work() -> None:
            if not after or after == before:
                ui.restore_foreground(hwnd)
                return
            rules = worker.vocab.learn(before, after)
            if replace:
                ui.restore_foreground(hwnd)
                time.sleep(0.25)
                ui.press_backspace(len(before))
                type_text(after, method=output_method)
            else:
                ui.restore_foreground(hwnd)
            _last["text"] = after
            if rules:
                _indicator.info("已记住：" + "；".join(f"{a} → {b}" for a, b in rules[:3]), 3.5)
            else:
                _indicator.info("已改好（没有可记住的词）")

        # 别在 Tk 线程里按键、等待
        threading.Thread(target=work, daemon=True).start()

    _indicator.correct(before, done)


def _register_hold(key: str, worker: TranscriptionWorker) -> None:
    """按住说话：按下开始录音，松开结束。按住时键盘会连发按下事件，只认第一次。
    这个键被吞掉，不再传给当前程序（F2 在资源管理器里是重命名）。"""
    held = {"down": False}
    # 吞键的回调是在 Windows 键盘钩子里同步跑的：处理慢了 Windows 会悄悄摘掉钩子，
    # 之后按键再也收不到。所以这里只记下按下/松开，开始、结束录音交给另一个线程。
    events: "queue.Queue[bool]" = queue.Queue()

    def on_key(event) -> None:
        if event.event_type == keyboard.KEY_DOWN:
            if held["down"]:
                return
            held["down"] = True
            events.put(True)
        else:
            held["down"] = False
            events.put(False)

    def run() -> None:
        while True:
            down = events.get()
            try:
                if down and not worker.is_running:
                    _toggle(worker, debounce=False)
                elif not down and worker.is_running:
                    _toggle(worker, debounce=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("处理热键出错: %s", exc, exc_info=True)

    threading.Thread(target=run, daemon=True, name="hold-key").start()
    keyboard.hook_key(key, on_key, suppress=True)
    logger.info("已注册按住说话热键 %s", key)


def _watch_hook(worker: TranscriptionWorker) -> None:
    """键盘钩子看门狗：两分钟没收到任何按键，就自己发一个 F24（没有程序用它）看钩子还在不在；
    收不到说明被 Windows 摘掉了，重新启动自己。"""
    import subprocess
    import sys

    seen = {"t": time.monotonic()}
    keyboard.hook(lambda e: seen.__setitem__("t", time.monotonic()))

    def loop() -> None:
        while True:
            time.sleep(30)
            if time.monotonic() - seen["t"] < 120 or worker.is_running:
                continue
            before = seen["t"]
            ui.tap_key(0x87)  # F24
            time.sleep(2)
            if seen["t"] != before:
                continue
            logger.error("键盘钩子没反应了（可能被 Windows 摘掉），重新启动 VocoType")
            flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen([sys.executable] + sys.argv, cwd=os.path.dirname(os.path.abspath(__file__)),
                             creationflags=flags, close_fds=True)
            os._exit(0)

    threading.Thread(target=loop, daemon=True, name="hook-watchdog").start()


def _toggle(worker: TranscriptionWorker, debounce: bool = True) -> None:
    global _last_toggle_time
    now = time.monotonic()
    with _toggle_lock:
        if debounce and now - _last_toggle_time < _TOGGLE_DEBOUNCE_SECONDS:
            logger.debug("忽略快速重复的录音切换请求 (%.3fs)", now - _last_toggle_time)
            return
        _last_toggle_time = now

    if worker.is_running:
        # 停止录音，提交转录任务
        worker.stop()
        if _indicator:
            _indicator.busy()
        stats = worker.transcription_stats
        if stats["pending"] > 0:
            logger.info(
                "录音已停止并提交转录，队列中还有 %d 个任务等待处理",
                stats["pending"]
            )
    else:
        # 开始录音
        stats = worker.transcription_stats
        if stats["pending"] > 0:
            logger.info(
                "开始录音（后台还有 %d 个转录任务正在处理）",
                stats["pending"]
            )
        worker.start()
        if _indicator:
            _indicator.recording()


if __name__ == "__main__":
    main()

