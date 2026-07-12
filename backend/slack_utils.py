import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def load_local_env():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def send_slack_message(db=None, text: str = "", blocks: Optional[list] = None, channel: Optional[str] = None) -> dict:
    """Envía mensaje a Slack usando la configuración guardada en Mongo o env vars."""
    load_local_env()

    settings = {}
    if db is not None:
        try:
            settings = await db.settings.find_one({"_id": "slack_config"}) or {}
        except Exception as exc:
            logger.warning("No se pudo leer configuracion de Slack desde Mongo: %s", exc)

    token = settings.get("slack_token") or os.environ.get("SLACK_BOT_TOKEN", "")
    webhook_url = settings.get("slack_webhook_url") or os.environ.get("SLACK_WEBHOOK_URL", "")
    resolved_channel = channel or settings.get("slack_channel") or os.environ.get("SLACK_CHANNEL_ID", "")

    if not token and not webhook_url:
        logger.warning("Slack no configurado - mensaje no enviado")
        return {"ok": False, "error": "not_configured"}

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    if resolved_channel:
        payload["channel"] = resolved_channel

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if webhook_url and not (channel and token):
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code != 200:
                    logger.error("Slack webhook error HTTP %s: %s", resp.status_code, resp.text[:500])
                    return {"ok": False, "error": "webhook_http_error", "status_code": resp.status_code}
                return {"ok": True, "transport": "webhook"}

            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
            try:
                data = resp.json()
            except ValueError:
                logger.error("Slack devolvio una respuesta no JSON HTTP %s: %s", resp.status_code, resp.text[:500])
                return {"ok": False, "error": "invalid_response", "status_code": resp.status_code}

            if resp.status_code != 200:
                logger.error("Slack error HTTP %s: %s", resp.status_code, data.get("error"))
                return {"ok": False, "error": data.get("error") or "http_error", "status_code": resp.status_code}

            if not data.get("ok"):
                logger.error("Slack error: %s", data.get("error"))
                if data.get("error") == "missing_scope" and webhook_url:
                    webhook_resp = await client.post(webhook_url, json=payload)
                    if webhook_resp.status_code == 200:
                        return {"ok": True, "fallback": "webhook"}
                    return {
                        "ok": False,
                        "error": "webhook_http_error",
                        "status_code": webhook_resp.status_code,
                        "fallback": "webhook",
                    }
            return data
    except httpx.HTTPError as exc:
        logger.error("Fallo de red enviando Slack: %s", exc)
        return {"ok": False, "error": "network_error", "detail": str(exc)}
