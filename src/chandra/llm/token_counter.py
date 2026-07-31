"""Token counting utility for Chandra LLM prompts.

Provides approximate and accurate token counting for prompt budget management.
Local LLMs have limited context (16K tokens), so we need to enforce budgets
before sending prompts to avoid silent truncation.
"""

import re

# Approximate token ratio (~4 chars per token for English)
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate token count using char/token ratio.

    Fast, suitable for pre-flight budget checks before calling the LLM.
    Actual token count may vary by ~20% depending on model tokenizer.
    """
    return max(1, len(text) // int(CHARS_PER_TOKEN))


def count_tokens_approximate(text: str) -> int:
    """Approximate token count using a simple heuristic.

    Uses the standard 4 chars per token ratio plus a small overhead
    for non-ASCII characters (which may take more tokens).
    """
    ascii_chars = len(re.findall(r"[\x00-\x7F]", text))
    non_ascii = len(text) - ascii_chars
    return max(1, (ascii_chars // 4) + (non_ascii // 2))


def check_prompt_budget(
    prompt: str,
    max_tokens: int = 12000,
    output_budget: int = 4000,
) -> dict:
    """Check if a prompt fits within the context budget.

    Returns a dict with:
    - ok: bool — whether the prompt fits
    - estimated_prompt_tokens: int
    - estimated_total_tokens: int (prompt + estimated output)
    - max_allowed: int
    - message: str — human-readable status
    """
    prompt_tokens = count_tokens_approximate(prompt)
    total_estimated = prompt_tokens + output_budget
    max_allowed = max_tokens + output_budget

    if total_estimated > max_allowed:
        return {
            "ok": False,
            "estimated_prompt_tokens": prompt_tokens,
            "estimated_total_tokens": total_estimated,
            "max_allowed": max_allowed,
            "message": (
                f"Prompt too large: ~{prompt_tokens} tokens (prompt) + "
                f"{output_budget} tokens (output) = ~{total_estimated} total, "
                f"exceeds {max_allowed} limit. Reduce task count or context."
            ),
        }

    return {
        "ok": True,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_total_tokens": total_estimated,
        "max_allowed": max_allowed,
        "message": f"Prompt fits: ~{prompt_tokens} + {output_budget} = ~{total_estimated} ≤ {max_allowed}",
    }


def truncate_to_budget(
    text: str,
    budget_chars: int,
    preserve_head: bool = True,
) -> str:
    """Truncate text to fit within a character budget (~4 chars/token).

    If preserve_head is True, keeps the beginning of the text (most important
    for LLM context). Otherwise keeps the end.
    """
    if len(text) <= budget_chars:
        return text

    if preserve_head:
        return text[:budget_chars] + f"\n... [truncated: {len(text) - budget_chars} chars omitted]"
    else:
        return f"... [truncated: {len(text) - budget_chars} chars omitted]\n" + text[-budget_chars:]
