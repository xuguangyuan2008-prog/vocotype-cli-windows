"""个人词库：纠错替换 + 热词，存在项目目录的 user_vocab.json。

- replace：{"识别错的": "正确的"}，识别结果输出前自动替换（长的先换）。
- hotwords：常用专业词、人名、产品名；交给支持热词的模型（Qwen3-ASR）让它听的时候往这些词上靠。

纠错窗口里把一句话改对后调用 learn(原句, 改后)，自动找出改动的片段记下来。
文件可以直接手改，改了下一句就生效（按修改时间重新读）。
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB_PATH = os.path.join(_ROOT, "user_vocab.json")

# 片段两边的标点、空白不算改动内容
_EDGE = re.compile(r"^[\s，。、！？；：,.!?;:\"'“”‘’()（）]+|[\s，。、！？；：,.!?;:\"'“”‘’()（）]+$")


class Vocab:
    def __init__(self, path: str = VOCAB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self.replace: dict[str, str] = {}
        self.hotwords: list[str] = []
        self._load()

    # ------------------------------------------------------------ 读写

    def _load(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.replace = {str(k): str(v) for k, v in (data.get("replace") or {}).items() if k}
            self.hotwords = [str(w) for w in (data.get("hotwords") or []) if str(w).strip()]
            self._mtime = mtime
            logger.info("已载入个人词库：%d 条替换，%d 个热词", len(self.replace), len(self.hotwords))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取个人词库 %s 失败: %s", self.path, exc)

    def _save(self) -> None:
        data = {
            "说明": "replace：识别错的 → 正确的，输出前自动替换；hotwords：常用词，让模型优先识别。可直接修改本文件。",
            "replace": self.replace,
            "hotwords": self.hotwords,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)

    # ------------------------------------------------------------ 使用

    def apply(self, text: str) -> str:
        with self._lock:
            self._load()
            for wrong in sorted(self.replace, key=len, reverse=True):
                if wrong in text:
                    text = text.replace(wrong, self.replace[wrong])
        return text

    def context(self) -> str:
        """给模型的热词提示（Qwen3-ASR 的 context）。"""
        with self._lock:
            self._load()
            words = list(dict.fromkeys(self.hotwords + list(self.replace.values())))
        return " ".join(words[-200:])

    def learn(self, before: str, after: str) -> list[tuple[str, str]]:
        """比较原句和改后的句子，把改动片段记成替换规则，新词记成热词。返回新增的规则。"""
        rules = diff_rules(before, after)
        if not rules:
            return []
        with self._lock:
            self._load()
            for wrong, right in rules:
                self.replace[wrong] = right
                if right not in self.hotwords and len(right) >= 2:
                    self.hotwords.append(right)
            self._save()
        logger.info("记住纠错：%s", "；".join(f"{a} → {b}" for a, b in rules))
        return rules

    def add_hotword(self, word: str) -> None:
        word = word.strip()
        if not word:
            return
        with self._lock:
            self._load()
            if word not in self.hotwords:
                self.hotwords.append(word)
                self._save()


def diff_rules(before: str, after: str) -> list[tuple[str, str]]:
    """按字比较，取出「错 → 对」的片段。

    只改一个字的，往两边各带一个字再记（只记「的 → 得」会把所有的「的」都换掉）。
    只改标点、只删不加的不记。
    """
    if before == after:
        return []
    sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
    rules: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or tag == "delete":
            continue
        # 片段太短就往两边扩，扩出来的字两边相同
        while (i2 - i1) < 2 and (i1 > 0 or i2 < len(before)):
            grew = False
            if i1 > 0 and j1 > 0 and before[i1 - 1] == after[j1 - 1]:
                i1 -= 1
                j1 -= 1
                grew = True
            if (i2 - i1) < 2 and i2 < len(before) and j2 < len(after) and before[i2] == after[j2]:
                i2 += 1
                j2 += 1
                grew = True
            if not grew:
                break
        wrong = _EDGE.sub("", before[i1:i2])
        right = _EDGE.sub("", after[j1:j2])
        if len(wrong) < 2 or not right or wrong == right:
            continue
        rules.append((wrong, right))
    return rules
