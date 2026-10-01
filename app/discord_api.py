"""Appels OAuth Discord (scope 'identify' uniquement)."""
from urllib.parse import urlencode

import httpx

from . import config


def authorize_url(state: str) -> str:
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code",
        "scope": config.OAUTH_SCOPE,
        "state": state,
        "prompt": "consent",
    }
    return f"{config.DISCORD_API}/oauth2/authorize?" + urlencode(params)


async def exchange_code(code: str) -> dict:
    data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{config.DISCORD_API}/oauth2/token", data=data,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()


async def get_current_user(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{config.DISCORD_API}/users/@me",
                        headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()
