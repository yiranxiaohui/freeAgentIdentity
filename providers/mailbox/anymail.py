"""AnyMail code-reception mailbox — register into unified registry."""
from core.anymail_mailbox import AnyMailPool  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "anymail")(AnyMailPool)
