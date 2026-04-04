#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class Logger:
    def __init__(self, text_path: Optional[str], jsonl_path: Optional[str]) -> None:
        self.text_path = Path(text_path).resolve() if text_path else None
        self.jsonl_path = Path(jsonl_path).resolve() if jsonl_path else None

        if self.text_path:
            self.text_path.parent.mkdir(parents=True, exist_ok=True)

        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def write(self, tag: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        line = f"[{self.now_str()}] [{tag}] {message}"
        print(line, flush=True)

        if self.text_path:
            with self.text_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        if self.jsonl_path and payload is not None:
            obj = {
                "ts_local": self.now_str(),
                "tag": tag,
                "message": message,
                "payload": payload,
            }
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")