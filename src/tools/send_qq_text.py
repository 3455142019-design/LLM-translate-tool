# -*- coding: utf-8 -*-
"""send_qq_text.py — 通过 QQ Bot REST API 主动推送文本消息给用户。

凭证来源：优先环境变量，其次 ~/AppData/Local/hermes/.env（QQ_APP_ID/
QQ_CLIENT_SECRET/QQBOT_HOME_CHANNEL），最后 config.yaml gateway.qqbot.extra。

用法：python send_qq_text.py "消息内容"
（内容较长时分多段发送，每段不超过 QQ 限制；只发文本，够用）
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

HOME = Path.home()
ENV_FILE = HOME / "AppData/Local/hermes" / ".env"
CONFIG_FILE = HOME / "AppData/Local/hermes" / "config.yaml"


def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def _load_config_extra() -> dict:
    """从 config.yaml 提取 qqbot 相关配置。

    返回 {app_id, client_secret, home_channel}：
    - app_id/client_secret 在 gateway.platforms.qqbot.extra
    - home_channel 在 gateway.platforms.qqbot.extra.home_channel 或顶层 QQBOT_HOME_CHANNEL
    """
    if not CONFIG_FILE.exists():
        return {}
    text = CONFIG_FILE.read_text(encoding="utf-8", errors="ignore")
    out = {}
    try:
        import yaml
        cfg = yaml.safe_load(text)
        extra = (cfg.get("gateway", {}).get("platforms", {})
                 .get("qqbot", {}).get("extra", {}) or {})
        out["app_id"] = str(extra.get("app_id", ""))
        out["client_secret"] = str(extra.get("client_secret", ""))
        out["home_channel"] = str(extra.get("home_channel", "")
                                  or cfg.get("QQBOT_HOME_CHANNEL", ""))
    except ImportError:
        # 无 yaml 库时用简易文本提取
        for key in ("app_id", "client_secret"):
            m = re.search(rf"{key}\s*:\s*[\"']?([^\"'\n]+)", text)
            if m:
                out[key] = m.group(1).strip()
        m = re.search(r"home_channel\s*:\s*[\"']?([^\"'\n]+)", text)
        if m:
            out["home_channel"] = m.group(1).strip()
        m = re.search(r"^QQBOT_HOME_CHANNEL\s*:\s*[\"']?([^\"'\n]+)", text, re.M)
        if m and not out.get("home_channel"):
            out["home_channel"] = m.group(1).strip()
    return out


def _get_credentials() -> tuple:
    env = _load_env()
    cfg = _load_config_extra()
    app_id = (os.environ.get("QQ_APP_ID") or env.get("QQ_APP_ID")
              or cfg.get("app_id", ""))
    secret = (os.environ.get("QQ_CLIENT_SECRET") or env.get("QQ_CLIENT_SECRET")
              or cfg.get("client_secret", ""))
    openid = (os.environ.get("QQBOT_HOME_CHANNEL")
              or env.get("QQBOT_HOME_CHANNEL")
              or cfg.get("home_channel", ""))
    if not all([app_id, secret, openid]):
        raise SystemExit(
            f"缺少凭证: app_id={'有' if app_id else '无'} secret={'有' if secret else '无'} "
            f"openid={'有' if openid else '无'}")
    return app_id, secret, openid


async def _send_one(client: httpx.AsyncClient, token: str, openid: str,
                    text: str) -> int:
    r = await client.post(
        f"https://api.sgroup.qq.com/v2/users/{openid}/messages",
        json={"content": text, "msg_type": 0},
        headers={"Authorization": f"QQBot {token}",
                 "Content-Type": "application/json"},
        timeout=20)
    return r.status_code


async def main(text: str) -> int:
    app_id, secret, openid = _get_credentials()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": app_id, "clientSecret": secret})
        if r.status_code != 200:
            print(f"获取 access token 失败: HTTP {r.status_code} {r.text[:200]}")
            return 1
        token = r.json()["access_token"]

        # QQ 单条消息限制约 4000 字（UTF-16），按 1500 字符安全分段
        chunks = [text[i:i + 1500] for i in range(0, len(text), 1500)]
        ok = 0
        for i, chunk in enumerate(chunks, 1):
            code = await _send_one(client, token, openid, chunk)
            status = "OK" if code in (200, 201) else f"失败 HTTP {code}"
            print(f"[{i}/{len(chunks)}] {status}")
            if code in (200, 201):
                ok += 1
            await asyncio.sleep(1)  # 防频控
        return 0 if ok == len(chunks) else 1


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else ""
    if not msg:
        raise SystemExit("用法: python send_qq_text.py '消息内容'")
    sys.exit(asyncio.run(main(msg)))
