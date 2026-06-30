#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度翻译 API 通用翻译客户端

使用方式：
    from translator import BaiduTranslator

    tx = BaiduTranslator()  # 自动从环境变量 BAIDU_APPID / BAIDU_TRANSLATE_KEY 读取
    zh = tx.translate("Open source video production system", to="zh")

环境变量：
    BAIDU_APPID            必填，开发者信息里的应用 ID
    BAIDU_TRANSLATE_KEY    必填，密钥（你给的 yvxV_d8t0bs8kiv21q30o0jg0）

QPS 限制：
    标准版 QPS=1，所以批量翻译每条间隔 ~1.1s，避免触发 54003 限流。
"""

import os
import time
import random
import string
import hashlib
import logging
from urllib.parse import urlencode

import requests

API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
DEFAULT_TIMEOUT = 10
QPS_SLEEP = 1.1  # 标准版 QPS=1，留点余量

log = logging.getLogger("translator")


class BaiduTranslator:
    def __init__(self, appid: str = None, key: str = None, timeout: int = DEFAULT_TIMEOUT):
        self.appid = appid or os.environ.get("BAIDU_APPID", "").strip()
        self.key = key or os.environ.get("BAIDU_TRANSLATE_KEY", "").strip()
        self.timeout = timeout
        self.session = requests.Session()

        if not self.appid or not self.key:
            raise ValueError(
                "百度翻译凭据缺失：需设置环境变量 BAIDU_APPID 和 BAIDU_TRANSLATE_KEY"
            )

    def _make_sign(self, q: str, salt: str) -> str:
        # 注意：拼接 sign 时 q 不要 URL encode（百度官方文档明确要求）
        raw = self.appid + q + salt + self.key
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def translate(self, text: str, from_lang: str = "auto", to_lang: str = "zh",
                  retries: int = 2) -> str | None:
        """翻译单条文本。返回译文；失败返回 None（不抛异常）。"""
        if not text or not text.strip():
            return ""

        text = text.strip()
        for attempt in range(retries + 1):
            try:
                salt = "".join(random.choices(string.ascii_letters + string.digits, k=8))
                sign = self._make_sign(text, salt)

                params = {
                    "q": text,
                    "from": from_lang,
                    "to": to_lang,
                    "appid": self.appid,
                    "salt": salt,
                    "sign": sign,
                }
                resp = self.session.post(API_URL, data=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()

                if "error_code" in data:
                    err = data.get("error_code")
                    err_msg = data.get("error_msg", "")
                    log.warning(f"翻译失败 code={err} msg={err_msg} | text={text[:50]!r}")
                    if err in ("54001", "54003", "58003"):
                        # 签名/QPS/风控问题，不重试
                        return None
                    # 其它错误重试一次
                    time.sleep(QPS_SLEEP)
                    continue

                result = data.get("trans_result") or []
                if result and result[0].get("dst"):
                    return result[0]["dst"]

                return None
            except (requests.RequestException, ValueError) as e:
                log.warning(f"翻译请求异常 attempt={attempt + 1}: {e}")
                if attempt < retries:
                    time.sleep(QPS_SLEEP)
                    continue
                return None
        return None

    def translate_batch(self, texts: list[str], to_lang: str = "zh") -> list[str | None]:
        """串行翻译多条文本（遵守 QPS=1 限制）。空字符串原样返回。"""
        results: list[str | None] = []
        for i, t in enumerate(texts):
            if i > 0:
                time.sleep(QPS_SLEEP)
            translated = self.translate(t, to_lang=to_lang)
            results.append(translated)
        return results


def translate_repo_descriptions(repos: list[dict], to_lang: str = "zh",
                                skip_translated: bool = True) -> list[dict]:
    """给仓库列表批量翻译 description，添加 description_zh 字段。

    行为：
    - 若 description 为空/已存在 description_zh 且 skip_translated=True，跳过
    - 翻译失败时 description_zh 设为 None（前端 fallback 用英文）
    - 原地修改 repos 并返回
    """
    try:
        tx = BaiduTranslator()
    except ValueError as e:
        log.warning(str(e))
        return repos

    for repo in repos:
        desc = (repo.get("description") or "").strip()
        if not desc:
            repo["description_zh"] = ""
            continue
        if skip_translated and repo.get("description_zh"):
            continue

        zh = tx.translate(desc, to_lang=to_lang)
        repo["description_zh"] = zh if zh else None
        log.info(f"翻译 [{repo.get('author', '?')}/{repo.get('name', '?')}] "
                 f"{'✓' if zh else '✗'}")

    return repos


if __name__ == "__main__":
    # 简单自测
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    samples = [
        "Open source agentic video production system",
        "Native macOS video editor built for AI workflows",
        "GitHub's largest open-source PDF app",
    ]
    try:
        out = translate_repo_descriptions(
            [{"description": s, "description_zh": "", "author": "x", "name": "y"} for s in samples]
        )
        for o in out:
            print(f"\nEN: {o['description']}\nZH: {o['description_zh']}")
    except ValueError as e:
        print(f"跳过自测：{e}")