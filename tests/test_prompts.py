"""The system prompt: fixed instructions, then the user's stored preferences."""

from __future__ import annotations

from team_assistant.prompts import SYSTEM_PROMPT, system_prompt


def test_without_preferences_the_prompt_is_the_fixed_text():
    assert system_prompt([]) == SYSTEM_PROMPT


def test_preferences_follow_the_fixed_text_so_its_start_never_changes():
    prompt = system_prompt(["Use Python for code.", "Keep examples short."])
    assert prompt.startswith(SYSTEM_PROMPT)
    block = prompt[len(SYSTEM_PROMPT) :]
    assert "<user_preferences>\n- Use Python for code.\n- Keep examples short.\n" in block
    assert "unless the user says otherwise" in block


def test_the_prompt_asks_for_one_search_and_no_announcement():
    assert "Put everything the user asked into one query" in SYSTEM_PROMPT
    assert "Do not announce a search" in SYSTEM_PROMPT
    assert "do not make one up" in SYSTEM_PROMPT


def test_the_prompt_names_every_tool_the_model_has():
    for name in ("list_issues", "get_issue", "search_memory", "recall_conversations"):
        assert name in SYSTEM_PROMPT
