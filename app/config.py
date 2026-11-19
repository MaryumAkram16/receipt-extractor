"""Small helpers for reading feature flags from the environment.

Read at call time (not import time) so tests and operators can flip a flag
without restarting the interpreter.
"""
import os

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def llm_enabled() -> bool:
    """Kill switch. LLM_ENABLED=false (or 0/no/off) disables every model call."""
    return env_flag("LLM_ENABLED", True)


def llm_stub() -> bool:
    """LLM_STUB=1 (or true/yes/on) returns a fixed result with zero model calls."""
    return env_flag("LLM_STUB", False)


def llm_mode() -> str:
    """Which extraction path is active: 'disabled', 'stub' or 'live'.

    Exposed on /health so evaluation tooling can refuse to score a stub.
    The kill switch wins over stub mode, matching run_extraction's order.
    """
    if not llm_enabled():
        return "disabled"
    if llm_stub():
        return "stub"
    return "live"


def diagnostic_log_content() -> bool:
    """Opt-in: include receipt text and raw model output in quarantine logs.

    Off by default — receipts are financial data, and diagnostic files tend
    to be copied around far more freely than the database.
    """
    return env_flag("DIAGNOSTIC_LOG_CONTENT", False)
