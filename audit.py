"""Acting-identity capture for the audit log.

Service write ops in `services.py` don't take an actor argument — they'd all need
threading through two route layers. Instead a request-scoped ContextVar carries
who is acting; `ActorMiddleware` (main.py) sets it from the session/token at the
start of each request, and `services.record_audit` reads it. Defaults to a system
actor so scripts, the cron, snapshot restores, and tests never crash for lack of a
request context.
"""

import contextvars

# (actor display, actor_kind) where actor_kind ∈ {"manager", "admin", "system"}.
_DEFAULT: tuple[str, str] = ("system", "system")
_actor: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "audit_actor", default=_DEFAULT
)


def set_actor(actor: str, kind: str) -> contextvars.Token:
    """Set the acting identity for the current context; returns a reset token."""
    return _actor.set((actor, kind))


def reset_actor(token: contextvars.Token) -> None:
    _actor.reset(token)


def current_actor() -> tuple[str, str]:
    """(actor, actor_kind) for the current request, or the system default."""
    return _actor.get()
