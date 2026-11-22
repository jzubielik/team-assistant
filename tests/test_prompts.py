"""The system prompt: fixed instructions, then the user's stored preferences."""

from __future__ import annotations

from team_assistant.github import github_tools
from team_assistant.prompts import SYSTEM_PROMPT, system_prompt
from team_assistant.recall import memory_tools


def test_without_preferences_the_prompt_is_the_fixed_text():
    assert system_prompt([]) == SYSTEM_PROMPT


def test_preferences_follow_the_fixed_text_so_its_start_never_changes():
    prompt = system_prompt(["Use Python for code.", "Keep examples short."])
    assert prompt.startswith(SYSTEM_PROMPT)
    block = prompt[len(SYSTEM_PROMPT) :]
    assert "<user_preferences>\n- Use Python for code.\n- Keep examples short.\n" in block
    assert "unless the user says otherwise" in block


def test_the_prompt_asks_for_no_announcement():
    assert "Do not announce a search" in SYSTEM_PROMPT


def test_the_prompt_allows_searching_again_and_names_the_limit():
    assert (
        "a differently worded query can return other facts: search again, worded "
        "differently, when the result lacks a part of what was asked. "
        "You can call tools at most 5 times in a row (several tools called at once count as "
        "one time); then answer with what you have."
    ) in SYSTEM_PROMPT


def test_the_prompt_forbids_making_anything_up():
    assert "Never make anything up: when you are not sure or do not know, say so." in SYSTEM_PROMPT
    # What the model knows about the user's own matters has three sources and no other.
    assert (
        "About this user, their team and their systems you know only what this conversation, "
        "a tool result or the preferences in this prompt say."
    ) in SYSTEM_PROMPT


def test_the_prompt_names_every_tool_the_model_has():
    tools = [*github_tools(lambda url: None), *memory_tools(None, "mem-1", "u-1", 5, 5)]
    names = [tool.tool_name for tool in tools]
    assert names
    for name in names:
        assert name in SYSTEM_PROMPT
