"""cloudflare_temp_email code-reception mailbox — register into unified registry."""
from core.cftemp_mailbox import CloudflareTempEmailPool  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "cftemp")(CloudflareTempEmailPool)
