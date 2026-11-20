from urllib.parse import urlencode

import httpx


ORIGIN = "https://open.douyin.com"


class DouyinError(Exception):
    """向面板返回受控错误，禁止传回平台原始响应。"""


def authorization_url(key: str, redirect: str, state: str) -> str:
    return ORIGIN + "/platform/oauth/connect/?" + urlencode({
        "client_key": key, "response_type": "code", "scope": "user_info",
        "redirect_uri": redirect, "state": state,
    })


class DouyinClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def _post(self, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(base_url=ORIGIN, trust_env=False, timeout=10,
                                         transport=self.transport) as client:
                response = await client.post(path, follow_redirects=False, **kwargs)
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError):
            raise DouyinError("抖音官方接口暂不可用，请稍后检查。") from None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise DouyinError("抖音接口返回的格式无法识别。")
        code = data.get("error_code")
        if str(code) != "0":
            code = str(code)
            suffix = f"（错误码 {code}）" if code.isascii() and code.isdigit() and len(code) <= 10 else ""
            raise DouyinError(f"抖音拒绝了请求{suffix}。请检查应用权限或重新授权。")
        return data

    async def exchange(self, key: str, secret: str, code: str) -> dict:
        return await self._post("/oauth/access_token/", data={
            "client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code",
        })

    async def refresh(self, key: str, refresh_token: str) -> dict:
        fields = {"client_key": key, "refresh_token": refresh_token, "grant_type": "refresh_token"}
        return await self._post("/oauth/refresh_token/", files={name: (None, value) for name, value in fields.items()})

    async def profile(self, token: str, open_id: str) -> dict:
        return await self._post("/oauth/userinfo/", headers={"access-token": token}, json={"open_id": open_id})
