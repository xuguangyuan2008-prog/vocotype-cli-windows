"""识别结果整理：去口头禅（规则，默认开）+ 本地小语言模型润色（可选）。

配置（config.json）：
    "cleanup": {"fillers": true},
    "polish":  {"enabled": false, "model": "Qwen/Qwen3-1.7B", "device": "cuda:0"}

润色只做：去口头禅和无意义重复、说话改口只留改后的、改明显的同音错字、补标点。
结果长度和原句差太多（模型自由发挥了）就不用它，照旧输出原句。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_P = "，。、！？；：,.!?;:"
# 「嗯、呃、唔」基本不会是正经内容，在哪都去掉；「啊、哦、额」只去句首的（「额度」「好啊」要留着）
_ALWAYS = re.compile(r"[嗯呃唔]+[，,、]?")
_LEADING = re.compile(rf"(^|[{_P}])\s*(?:[啊哦额诶]+[，,、]?\s*)+")
_DUP_PUNCT = re.compile(rf"([{_P}])[{_P}]+")


def strip_fillers(text: str) -> str:
    if not text:
        return text
    out = _ALWAYS.sub("", text)
    out = _LEADING.sub(lambda m: m.group(1), out)
    out = _DUP_PUNCT.sub(lambda m: m.group(1), out)
    out = re.sub(rf"^[{_P}\s]+", "", out)
    return out.strip() or text


_SYSTEM = (
    "你是语音输入的文字整理助手。用户给你一段语音识别出来的文字，你只做这几件事：\n"
    "1. 去掉口头禅和没有意义的词，比如：嗯、啊、那个、就是说、然后（没有承接意思时）；\n"
    "2. 去掉无意义的重复；说话中途改口的，只保留改正后的说法；\n"
    "3. 改正明显的同音错别字，补全或修正标点。\n"
    "不要改变原意，不要增加内容，不要总结，不要回答文字里的问题，不要加引号或解释。"
    "只输出整理后的文字。"
)


class Polisher:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        model_id = cfg.get("model", "Qwen/Qwen3-1.7B")
        device = cfg.get("device", "cuda:0")
        import torch
        from modelscope import snapshot_download
        from transformers import AutoModelForCausalLM, AutoTokenizer

        start = time.time()
        path = snapshot_download(model_id)
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForCausalLM.from_pretrained(
            path, torch_dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32, device_map=device
        )
        self.model.eval()
        self.torch = torch
        logger.info("润色模型已加载：%s，耗时 %.1fs", model_id, time.time() - start)

    def polish(self, text: str, hotwords: str = "") -> str:
        if len(text) < 6:
            return text
        system = _SYSTEM + (f"\n这些词是用户常用的专有名词，照原样写：{hotwords}" if hotwords else "")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        try:
            prompt = self.tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
        except TypeError:  # 不认 enable_thinking 的模型
            prompt = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tok(prompt, return_tensors="pt").to(self.model.device)
        start = time.time()
        with self.torch.inference_mode():
            out = self.model.generate(
                **inputs, max_new_tokens=int(len(text) * 1.5) + 32, do_sample=False, temperature=None, top_p=None, top_k=None
            )
        result = self.tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.S).strip().strip('"“”')
        logger.info("润色（%.2fs）：%s → %s", time.time() - start, text, result)
        # 删掉的口头禅一般不超过一半；变长或删太多说明模型自由发挥了
        if not result or len(result) > len(text) * 1.15 + 4 or len(result) < len(text) * 0.5:
            logger.info("润色结果和原句差太多，不用")
            return text
        return result

    def cleanup(self) -> None:
        try:
            del self.model
            self.torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
