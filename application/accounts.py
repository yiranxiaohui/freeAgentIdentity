from __future__ import annotations

import ast
import csv
import json
import re

from core.datetime_utils import serialize_datetime
from domain.accounts import (
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)
from infrastructure.accounts_repository import AccountsRepository


IMPORT_LINE_RE = re.compile(
    r'^\s*(?P<email>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'\s+(?P<password>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'(?:\s+(?P<extra>.*))?\s*$'
)


def _decode_import_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
            return decoded if isinstance(decoded, str) else str(decoded)
        except Exception:
            return text[1:-1]
    return text


def _parse_csv_row(raw: str) -> list[str]:
    return next(csv.reader([raw]))


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "items": [self._serialize(item) for item in items],
        }

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

    def delete_invalid_accounts(self) -> dict:
        """一键删除失效账号（validity/lifecycle 为 invalid），同时从 Sub2API 池删除。"""
        from application.account_exports import _sub2api_config, delete_sub2api_account_by_email
        from domain.accounts import AccountExportSelection

        records = self.repository.select_for_export(
            AccountExportSelection(platform="chatgpt", select_all=True, status_filter="invalid")
        )
        base_url, api_key = _sub2api_config()
        remote_enabled = bool(base_url and api_key)

        local_deleted = 0
        remote_deleted = 0
        remote_failed = 0
        errors: list[str] = []
        for rec in records:
            # 先删远端（sub2api），best-effort；失败不影响本地删除。
            if remote_enabled:
                try:
                    if delete_sub2api_account_by_email(base_url, api_key, rec.email):
                        remote_deleted += 1
                except Exception as exc:  # noqa: BLE001
                    remote_failed += 1
                    errors.append(f"{rec.email}: 远端删除失败 {exc}")
            try:
                if self.repository.delete(rec.id):
                    local_deleted += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rec.email}: 本地删除失败 {exc}")

        return {
            "total": len(records),
            "local_deleted": local_deleted,
            "remote_deleted": remote_deleted,
            "remote_failed": remote_failed,
            "remote_enabled": remote_enabled,
            "errors": errors[:20],
        }

    def import_accounts(self, platform: str, lines: list[str]) -> dict:
        parsed: list[AccountImportLine] = []
        csv_header: list[str] | None = None
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            if csv_header is None and "," in raw:
                try:
                    header_candidate = [item.strip().lower() for item in _parse_csv_row(raw)]
                except Exception:
                    header_candidate = []
                if "email" in header_candidate and "password" in header_candidate:
                    csv_header = header_candidate
                    continue
            if csv_header is not None:
                try:
                    values = _parse_csv_row(raw)
                except Exception:
                    values = []
                if values:
                    row = {
                        csv_header[index]: values[index]
                        for index in range(min(len(csv_header), len(values)))
                    }
                    email = str(row.get("email", "") or "").strip()
                    password = str(row.get("password", "") or "")
                    if email and password and "@" in email and " " not in email:
                        extra = {}
                        cashier_url = str(row.get("cashier_url", "") or "").strip()
                        if cashier_url:
                            extra["cashier_url"] = cashier_url
                        parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                        continue
            match = IMPORT_LINE_RE.match(raw)
            if not match:
                continue
            email = _decode_import_token(match.group("email"))
            password = _decode_import_token(match.group("password"))
            extra = {}
            payload = (match.group("extra") or "").strip()
            if payload:
                try:
                    decoded = json.loads(payload)
                    if isinstance(decoded, dict):
                        extra = decoded
                    elif decoded not in (None, ""):
                        extra = {"cashier_url": str(decoded)}
                except Exception:
                    extra = {"cashier_url": _decode_import_token(payload)}
            parsed.append(AccountImportLine(email=email, password=password, extra=extra))
        return {"created": self.repository.import_lines(platform, parsed)}

    def get_stats(self) -> dict:
        stats: AccountStats = self.repository.stats()
        return {
            "total": stats.total,
            "by_platform": stats.by_platform,
            "by_status": stats.by_status,
            "by_lifecycle_status": stats.by_lifecycle_status,
            "by_plan_state": stats.by_plan_state,
            "by_validity_status": stats.by_validity_status,
            "by_display_status": stats.by_display_status,
        }

    @staticmethod
    def _serialize(item: AccountRecord) -> dict:
        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": item.password,
            "user_id": item.user_id,
            "primary_token": item.primary_token,
            "trial_end_time": item.trial_end_time,
            "cashier_url": item.cashier_url,
            "proxy": item.proxy,
            "lifecycle_status": item.lifecycle_status,
            "validity_status": item.validity_status,
            "plan_state": item.plan_state,
            "plan_name": item.plan_name,
            "display_status": item.display_status,
            "overview": item.overview,
            "display_summary": item.display_summary,
            "credentials": item.credentials,
            "provider_accounts": item.provider_accounts,
            "provider_resources": item.provider_resources,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at),
        }
