"""Qwen3-ASR 本地识别（显卡），支持把个人热词作为提示（context）交给模型。

配置（config.json）：
    "backend": "qwen3",
    "qwen3": {"model": "Qwen/Qwen3-ASR-1.7B", "device": "cuda:0", "language": null}
language 为 null 时自动判断（中英混说也行），也可以写 "Chinese" 固定中文。
模型从 ModelScope 下载并缓存，第一次用会下载。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


class QwenASR:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None, sample_rate: int = 16000) -> None:
        cfg = cfg or {}
        self.sample_rate = sample_rate
        self.language = cfg.get("language")
        model_id = cfg.get("model", "Qwen/Qwen3-ASR-1.7B")
        device = cfg.get("device", "cuda:0")

        import torch
        from modelscope import snapshot_download
        from qwen_asr import Qwen3ASRModel

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("没有可用的 CUDA 显卡")
        start = time.time()
        path = snapshot_download(model_id)
        self.model = Qwen3ASRModel.from_pretrained(
            path,
            dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
            device_map=device,
            max_inference_batch_size=1,
            max_new_tokens=int(cfg.get("max_new_tokens", 512)),
        )
        logger.info("Qwen3-ASR 已加载：%s（%s），耗时 %.1fs", model_id, device, time.time() - start)
        # 预热一次，第一句不会特别慢
        try:
            self.transcribe(np.zeros(sample_rate // 2, dtype=np.int16))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Qwen3-ASR 预热失败: %s", exc)

    def transcribe(self, samples: np.ndarray, context: str = "") -> Dict[str, Any]:
        audio = samples.astype(np.float32) / 32768.0 if samples.dtype == np.int16 else samples.astype(np.float32)
        duration = audio.size / self.sample_rate
        try:
            res = self.model.transcribe(
                audio=(audio, self.sample_rate),
                context=context or "",
                language=self.language,
            )
            text = (res[0].text if res else "") or ""
            return {"success": True, "text": text.strip(), "raw_text": text, "duration": duration, "confidence": 1.0}
        except Exception as exc:  # noqa: BLE001
            logger.error("Qwen3-ASR 识别失败: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def cleanup(self) -> None:
        try:
            import torch

            del self.model
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
