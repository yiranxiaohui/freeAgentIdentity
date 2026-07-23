"""Mailbox provider backed by a self-hosted cloudflare_temp_email instance.

dreamhunter2333/cloudflare_temp_email is a Cloudflare Workers temp-mail service.
Its API mints a fresh address on demand and returns a per-address JWT used to
read that address's inbox:

* ``get_email``   → ``POST /api/new_address``   (create address, returns jwt)
* ``wait_for_code`` → ``GET /api/parsed_mails``   (poll, server-parsed subject/text/html)
* ``wait_for_link`` → ``GET /api/parsed_mails``
* cleanup          → ``DELETE /api/delete_address``

Auth is a per-address JWT (``Authorization: Bearer <jwt>``) obtained at address
creation, not a global key.  Code extraction is done client-side, reusing the
proven api_mailbox extractor (skips #-hex colors, prefers labelled codes).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from urllib.parse import urlparse

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link

logger = logging.getLogger(__name__)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _clean_base_url(base_url: str) -> str:
    url = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("cloudflare_temp_email 地址无效，应形如 https://mail.example.com")
    return url


class CloudflareTempEmailPool(BaseMailbox):
    """Create-on-demand addresses served by a cloudflare_temp_email deployment."""

    def __init__(
        self,
        *,
        base_url: str = "",
        domain: str = "",
        email_prefix: str = "u",
        cf_token: str = "",
        code_pattern: str = "",
        delete_after_use: bool = False,
        poll_interval: float | str = 1.5,
        request_timeout: float | str = 15,
        proxy: str | None = None,
        session: requests.Session | None = None,
    ):
        self.base_url = _clean_base_url(base_url)
        self.domain = str(domain or "").strip().lstrip("@").lower()
        prefix = str(email_prefix or "u").strip().strip("._-") or "u"
        self.email_prefix = "".join(ch for ch in prefix if ch.isalnum()) or "u"
        self.cf_token = str(cf_token or "").strip()
        self.code_pattern = str(code_pattern or "").strip()
        self.delete_after_use = bool(delete_after_use)
        self.poll_interval = max(0.0, float(1.5 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(15 if request_timeout in (None, "") else request_timeout))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, config: dict) -> "CloudflareTempEmailPool":
        return cls(
            base_url=config.get("cftemp_base_url", ""),
            domain=config.get("cftemp_domain", ""),
            email_prefix=config.get("cftemp_email_prefix", "u"),
            cf_token=config.get("cftemp_cf_token", ""),
            code_pattern=config.get("cftemp_code_pattern", ""),
            delete_after_use=_truthy(config.get("cftemp_delete_after_use")),
            poll_interval=config.get("cftemp_poll_interval", 1.5),
            request_timeout=config.get("cftemp_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
        )

    # ── HTTP helpers ─────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _resolve_domain(self) -> str:
        if self.domain:
            return self.domain
        response = self.session.get(
            self._url("/api/settings"),
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
        domains = [str(d or "").strip().lstrip("@").lower() for d in (payload.get("domains") or [])]
        domains = [d for d in domains if d]
        if not domains:
            raise RuntimeError("cloudflare_temp_email 未返回可用域名，请在设置中填写固定域名")
        return domains[0]

    def _new_local_part(self) -> str:
        return f"{self.email_prefix}{secrets.token_hex(5)}"

    # ── BaseMailbox API ──────────────────────────────────────────────
    def get_email(self) -> MailboxAccount:
        domain = self._resolve_domain()
        last_error = ""
        for _attempt in range(5):
            name = self._new_local_part()
            body: dict = {"name": name, "domain": domain}
            if self.cf_token:
                body["cf_token"] = self.cf_token
            response = self.session.post(
                self._url("/api/new_address"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=body,
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {str(response.text or '')[:150]}"
                # 地址冲突等可重试新前缀；其它错误直接抛出。
                if response.status_code in (409, 400):
                    continue
                response.raise_for_status()
            payload = response.json() or {}
            jwt = str(payload.get("jwt") or "").strip()
            email = str(payload.get("address") or f"{name}@{domain}").strip()
            if not jwt:
                raise RuntimeError(f"cloudflare_temp_email 未返回 jwt: {last_error or email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={
                    "cftemp_jwt": jwt,
                    "cftemp_email": email,
                    "provider_account": {
                        "provider_type": "mailbox",
                        "provider_name": "cftemp",
                        "login_identifier": email,
                        "display_name": email,
                        "credentials": {"email": email, "jwt": jwt},
                        "metadata": {"source": "cftemp", "base_url": self.base_url},
                    },
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": "cftemp",
                        "resource_type": "mailbox",
                        "resource_identifier": email.lower(),
                        "handle": email,
                        "display_name": email,
                        "metadata": {"email": email, "domain": domain, "source": "cftemp"},
                    },
                },
            )
        raise RuntimeError(f"cloudflare_temp_email 创建地址失败: {last_error or '未知错误'}")

    def _jwt_of(self, account: MailboxAccount) -> str:
        extra = getattr(account, "extra", {}) or {}
        jwt = str(extra.get("cftemp_jwt") or "").strip()
        if not jwt:
            provider_account = dict(extra.get("provider_account") or {})
            jwt = str((provider_account.get("credentials") or {}).get("jwt") or "").strip()
        if not jwt:
            raise RuntimeError(f"cloudflare_temp_email 账号缺少 jwt: {account.email}")
        return jwt

    def _fetch_mails(self, account: MailboxAccount, limit: int = 10) -> list[dict]:
        response = self.session.get(
            self._url("/api/parsed_mails"),
            headers={"Authorization": f"Bearer {self._jwt_of(account)}", "Accept": "application/json"},
            params={"limit": str(limit), "offset": "0"},
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
        results = payload.get("results")
        return list(results) if isinstance(results, list) else []

    @staticmethod
    def _mail_bodies(mail: dict) -> str:
        parts = [str(mail.get("subject") or ""), str(mail.get("text") or ""), str(mail.get("html") or "")]
        return "\n".join(part for part in parts if part)

    @classmethod
    def _signature(cls, mail: dict) -> str:
        value = str(mail.get("id") or "").strip()
        if value:
            return f"id:{value}"
        digest = hashlib.sha256(cls._mail_bodies(mail).encode("utf-8")).hexdigest()
        return f"body:{digest}"

    @staticmethod
    def _extract_code_from_mail(mail: dict, code_pattern: str | None) -> str:
        from core.api_mailbox import ApiMailboxPool

        pattern = code_pattern or None
        # 先纯文本、再主题、最后 HTML（HTML 里的 #202123 等色值会被朴素 \d{6} 误伤）。
        for body in (mail.get("text"), mail.get("subject"), mail.get("html")):
            text = str(body or "").strip()
            if not text:
                continue
            code = ApiMailboxPool._extract_code(None, text, pattern)
            if code:
                return code
        return ""

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._signature(m) for m in self._fetch_mails(account)}
        except Exception:
            return set()

    def _maybe_delete(self, account: MailboxAccount) -> None:
        if not self.delete_after_use:
            return
        try:
            self.session.delete(
                self._url("/api/delete_address"),
                headers={"Authorization": f"Bearer {self._jwt_of(account)}"},
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloudflare_temp_email 回收地址失败 (%s): %s", account.email, exc)

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        del keyword
        pattern = code_pattern or self.code_pattern or None
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                mails = self._fetch_mails(account)
                for mail in mails:
                    signature = self._signature(mail)
                    if signature in seen:
                        continue
                    code = self._extract_code_from_mail(mail, pattern)
                    if code:
                        self._maybe_delete(account)
                        return code
                seen.update(self._signature(m) for m in mails)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 cloudflare_temp_email 验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                mails = self._fetch_mails(account)
                for mail in mails:
                    signature = self._signature(mail)
                    if signature in seen:
                        continue
                    link = _extract_verification_link(self._mail_bodies(mail), keyword)
                    if link:
                        self._maybe_delete(account)
                        return link
                seen.update(self._signature(m) for m in mails)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 cloudflare_temp_email 验证链接超时 ({timeout}s){suffix}")
