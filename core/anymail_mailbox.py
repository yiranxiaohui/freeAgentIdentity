"""Mailbox provider backed by a self-hosted AnyMail instance.

AnyMail (Cloudflare Workers + D1) exposes a code-reception API: create a
throwaway domain mailbox on demand, then poll for the verification code which
the server extracts via a regex.  This maps directly onto :class:`BaseMailbox`:

* ``get_email``   → ``POST /api/accounts``      (create a random mailbox)
* ``wait_for_code`` → ``GET /api/emails/latest``  (poll + server-side regex)
* ``wait_for_link`` → ``GET /api/emails/latest``  (poll, extract first URL)

Unlike :mod:`core.api_mailbox` (a fixed pool of reusable addresses) each call
mints a fresh single-use mailbox, so no occupancy state file is needed.  See
``docs/code-reception.md`` in the AnyMail project for the full API contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
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
        raise ValueError("AnyMail base_url 无效，应形如 https://mail.example.com")
    return url


class AnyMailPool(BaseMailbox):
    """Create-on-demand domain mailboxes served by an AnyMail deployment."""

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        domain: str = "",
        email_prefix: str = "u",
        code_pattern: str = "",
        expires_minutes: float | str = 0,
        delete_after_use: bool = False,
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        proxy: str | None = None,
        session: requests.Session | None = None,
    ):
        self.base_url = _clean_base_url(base_url)
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("AnyMail api_key 不能为空")
        self.domain = str(domain or "").strip().lstrip("@").lower()
        prefix = str(email_prefix or "u").strip().strip("._-") or "u"
        self.email_prefix = re.sub(r"[^A-Za-z0-9]", "", prefix) or "u"
        self.code_pattern = str(code_pattern or "").strip()
        self.expires_minutes = max(0.0, float(0 if expires_minutes in (None, "") else expires_minutes))
        self.delete_after_use = bool(delete_after_use)
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(15 if request_timeout in (None, "") else request_timeout))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, config: dict) -> "AnyMailPool":
        return cls(
            base_url=config.get("anymail_base_url", ""),
            api_key=config.get("anymail_api_key", ""),
            domain=config.get("anymail_domain", ""),
            email_prefix=config.get("anymail_email_prefix", "u"),
            code_pattern=config.get("anymail_code_pattern", ""),
            expires_minutes=config.get("anymail_expires_minutes", 0),
            delete_after_use=_truthy(config.get("anymail_delete_after_use")),
            poll_interval=config.get("anymail_poll_interval", 3),
            request_timeout=config.get("anymail_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
        )

    # ── HTTP helpers ─────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "aBaiAutoplus/anymail",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _resolve_domain(self) -> str:
        if self.domain:
            return self.domain
        response = self.session.get(
            self._url("/api/domains"),
            headers=self._headers(),
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
        domains = [
            str((item or {}).get("name") or "").strip().lstrip("@").lower()
            for item in (payload.get("domains") or [])
        ]
        domains = [name for name in domains if name]
        if not domains:
            raise RuntimeError("AnyMail 未返回可用域名，请在设置中填写固定域名或在后台配置 EMAIL_DOMAINS")
        return domains[0]

    def _new_local_part(self) -> str:
        return f"{self.email_prefix}_{secrets.token_hex(5)}"

    # ── BaseMailbox API ──────────────────────────────────────────────
    def get_email(self) -> MailboxAccount:
        domain = self._resolve_domain()
        # Record `since` *before* creating so no mail in the race window is missed.
        since = datetime.now(timezone.utc).isoformat()
        expires_at = ""
        if self.expires_minutes > 0:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=self.expires_minutes)
            ).isoformat()

        last_error = ""
        for _attempt in range(5):
            email = f"{self._new_local_part()}@{domain}"
            body: dict = {"email": email}
            if expires_at:
                body["expires_at"] = expires_at
            response = self.session.post(
                self._url("/api/accounts"),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            if response.status_code == 409:
                last_error = "邮箱已存在，重试新前缀"
                continue
            response.raise_for_status()
            payload = response.json() or {}
            account = payload.get("account") or {}
            account_id = str(account.get("id") or "").strip()
            resolved_email = str(account.get("email") or email).strip()
            return MailboxAccount(
                email=resolved_email,
                account_id=account_id,
                extra={
                    "anymail_since": since,
                    "anymail_account_id": account_id,
                    "anymail_email": resolved_email,
                    "provider_account": {
                        "provider_type": "mailbox",
                        "provider_name": "anymail",
                        "login_identifier": resolved_email,
                        "display_name": resolved_email,
                        "credentials": {"email": resolved_email, "account_id": account_id},
                        "metadata": {"source": "anymail", "base_url": self.base_url},
                    },
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": "anymail",
                        "resource_type": "mailbox",
                        "resource_identifier": account_id or resolved_email.lower(),
                        "handle": resolved_email,
                        "display_name": resolved_email,
                        "metadata": {"email": resolved_email, "domain": domain, "source": "anymail"},
                    },
                },
            )
        raise RuntimeError(f"AnyMail 创建邮箱失败: {last_error or '未知错误'}")

    def _since_of(self, account: MailboxAccount) -> str:
        return str((getattr(account, "extra", {}) or {}).get("anymail_since") or "").strip()

    def _account_id_of(self, account: MailboxAccount) -> str:
        extra = getattr(account, "extra", {}) or {}
        account_id = str(extra.get("anymail_account_id") or account.account_id or "").strip()
        return account_id

    def _fetch_latest(
        self,
        account: MailboxAccount,
        *,
        with_code_regex: str | None,
        limit: int = 10,
    ) -> list[dict]:
        params = {"to": account.email, "limit": str(limit)}
        since = self._since_of(account)
        if since:
            params["since"] = since
        if with_code_regex:
            params["code_regex"] = with_code_regex
        response = self.session.get(
            self._url("/api/emails/latest"),
            headers=self._headers(),
            params=params,
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
        emails = payload.get("emails")
        return list(emails) if isinstance(emails, list) else []

    @staticmethod
    def _email_bodies(email: dict) -> str:
        parts = [
            str(email.get("subject") or ""),
            str(email.get("text_body") or ""),
            str(email.get("html_body") or ""),
        ]
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _extract_code_from_email(email: dict, code_pattern: str | None) -> str:
        """客户端提取验证码。

        不依赖 AnyMail 服务端的 ``code_regex`` —— Go RE2 不支持 lookbehind，
        朴素的 ``\\d{6}`` 会把 HTML 里 ``#202123`` 这类十六进制色值(OpenAI 邮件
        模板常见)误当成验证码。复用 api_mailbox 那套更稳的提取逻辑，并优先
        纯文本、其次主题、最后 HTML。
        """
        from core.api_mailbox import ApiMailboxPool

        pattern = code_pattern or None
        for body in (email.get("text_body"), email.get("subject"), email.get("html_body")):
            text = str(body or "").strip()
            if not text:
                continue
            code = ApiMailboxPool._extract_code(None, text, pattern)
            if code:
                return code
        return ""

    @classmethod
    def _signature(cls, email: dict) -> str:
        for key in ("id", "message_id"):
            value = str(email.get(key) or "").strip()
            if value:
                return f"id:{value}"
        digest = hashlib.sha256(cls._email_bodies(email).encode("utf-8")).hexdigest()
        return f"body:{digest}"

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            emails = self._fetch_latest(account, with_code_regex=None)
            return {self._signature(email) for email in emails}
        except Exception:
            return set()

    def _maybe_delete(self, account: MailboxAccount) -> None:
        if not self.delete_after_use:
            return
        account_id = self._account_id_of(account)
        if not account_id:
            return
        try:
            self.session.delete(
                self._url(f"/api/accounts/{account_id}"),
                headers=self._headers(),
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AnyMail 回收邮箱失败 (%s): %s", account_id, exc)

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        del keyword  # server filters by the requested mailbox directly
        pattern = code_pattern or self.code_pattern or None
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                # 取原始邮件后在客户端提码（见 _extract_code_from_email 说明）。
                emails = self._fetch_latest(account, with_code_regex=None)
                for email in emails:
                    signature = self._signature(email)
                    if signature in seen:
                        continue
                    code = self._extract_code_from_email(email, pattern)
                    if code:
                        self._maybe_delete(account)
                        return code
                seen.update(self._signature(email) for email in emails)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 AnyMail 验证码超时 ({timeout}s){suffix}")

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
                emails = self._fetch_latest(account, with_code_regex=None)
                for email in emails:
                    signature = self._signature(email)
                    if signature in seen:
                        continue
                    link = _extract_verification_link(self._email_bodies(email), keyword)
                    if link:
                        self._maybe_delete(account)
                        return link
                seen.update(self._signature(email) for email in emails)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 AnyMail 验证链接超时 ({timeout}s){suffix}")
