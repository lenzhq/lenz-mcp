"""MCP prompts: a visible button for people who do not know what to type.

claude.ai and Claude Desktop list a remote connector's prompts under "+" ->
Connectors -> "Add to <connector>" (modelcontextprotocol.io, "Connect to remote
MCP servers"), and Claude already asks for them on every connect: each
`Claude-User` handshake carries a `ListPromptsRequest`, which a connector with
no prompts answers with an empty list. Two prompts fill it.

A prompt cannot read the conversation, so the text to check is passed as an
argument. Titles are what a person sees (written for the person, not the model: no
tool names); the prompt text is what lands in the chat as the user's message.
The tools, their manifest and the server instructions are untouched.
"""

import asyncio

import pytest
from mcp.shared.exceptions import MCPError

from lenz_mcp import server
from lenz_mcp.testing import assembled_app


def _run(coro):
    return asyncio.run(coro)


def _prompts():
    return {p.name: p for p in _run(server.mcp.list_prompts())}


def _text_of(result):
    (message,) = result.messages
    assert message.role == 'user'
    return message.content.text


def test_the_server_declares_the_prompts_capability():
    """Read off the handshake a client actually gets: 2.x removed
    `FastMCP._mcp_server`, and `initialize`'s `capabilities` is what Claude reads
    to decide whether to ask for the prompt list at all."""
    with assembled_app(MCP_OAUTH_ENABLED=False) as harness:
        negotiated = harness.wire(user_agent='Claude-User/1.0').initialize()
    assert negotiated['capabilities'].get('prompts') is not None


def test_both_prompts_are_listed_with_title_description_and_one_required_argument():
    prompts = _prompts()
    assert set(prompts) == {'check_text', 'check_last_answer'}

    check_text = prompts['check_text']
    assert check_text.title == 'Check this text with Lenz'
    assert check_text.description == (
        'Check the factual claims in a text against independent sources: a draft, an article or any passage you paste.'
    )
    assert [(a.name, a.required) for a in check_text.arguments] == [('text', True)]
    assert check_text.arguments[0].description

    last_answer = prompts['check_last_answer']
    assert last_answer.title == 'Check your last answer with Lenz'
    assert last_answer.description == (
        'Check the factual claims in an answer your assistant gave against independent sources.'
    )
    assert [(a.name, a.required) for a in last_answer.arguments] == [('answer', True)]
    assert last_answer.arguments[0].description == (
        'Paste the answer you want checked. Lenz cannot see the conversation, so it needs the text here.'
    )


def test_titles_and_argument_descriptions_speak_to_a_person():
    """A person sees these in Claude's menu: no tool names, no "MCP" or "API"."""
    for prompt in _prompts().values():
        shown = [prompt.title, prompt.description, *(a.description for a in prompt.arguments)]
        for text in shown:
            for word in ('assess_claim', 'verify_claim', 'MCP', 'API'):
                assert word not in text, (prompt.name, word)


def test_check_text_returns_the_user_message_with_the_text_verbatim():
    text = 'Revenue grew 40% in 2025.\n\nThe company has 12 offices.'
    result = _run(server.mcp.get_prompt('check_text', {'text': text}))
    assert _text_of(result) == f'Check the claims in this text with Lenz:\n\n{text}'


def test_check_last_answer_returns_the_user_message_with_the_answer_verbatim():
    answer = 'The Eiffel Tower is 330 metres tall.'
    result = _run(server.mcp.get_prompt('check_last_answer', {'answer': answer}))
    assert _text_of(result) == f'Check with Lenz whether the factual claims in this answer are true:\n\n{answer}'


@pytest.mark.parametrize('name,arg', [('check_text', 'text'), ('check_last_answer', 'answer')])
def test_braces_and_format_markers_in_the_text_survive(name, arg):
    """The text is data, never a template: `{`, `{0}`, `%s` and `{{x}}` arrive as typed."""
    tricky = 'A {0} claim with {braces}, {{double}}, %s and %(name)s markers.'
    result = _run(server.mcp.get_prompt(name, {arg: tricky}))
    assert _text_of(result).endswith('\n\n' + tricky)


@pytest.mark.parametrize('name,arg', [('check_text', 'text'), ('check_last_answer', 'answer')])
@pytest.mark.parametrize('empty', ['', '   ', '\n\t'])
def test_an_empty_text_is_rejected_with_a_sentence_saying_what_to_paste(name, arg, empty):
    """`MCPError`, not `ValueError`: 2.x replaces a prompt's own exception text with
    a generic message and logs an ERROR traceback for it, so the sentence would
    never reach the person and every blank submission would be a Sentry event."""
    with pytest.raises(MCPError, match='Paste'):
        _run(server.mcp.get_prompt(name, {arg: empty}))


@pytest.mark.parametrize('name', ['check_text', 'check_last_answer'])
def test_a_missing_argument_is_rejected(name):
    with pytest.raises(ValueError):
        _run(server.mcp.get_prompt(name, {}))


def test_prompts_leave_the_tools_and_instructions_unchanged():
    tools = {t.name for t in _run(server.mcp.list_tools())}
    assert tools == {
        'assess_claim',
        'verify_claim',
        'select_claims',
        'get_verification',
        'get_verification_widget',
        'start_verification_widget',
        'select_claims_widget',
        'check_usage',
        'ask_followup',
        'list_verifications',
    }
    assert 'prompt' not in server.mcp.instructions.lower()


def test_prompts_are_served_over_the_http_transport_claude_uses():
    """End to end over Streamable HTTP, the transport claude.ai's connector calls,
    not just the MCPServer object the tests above drive.

    Through `assembled_app` rather than a hand-built transport: 2.x deleted
    `StreamableHTTPASGIApp`, and the harness drives `lenz_mcp.asgi.application`
    itself — the thing that is deployed — on a session manager of its own, which
    is what kept this test off the shared one."""
    with assembled_app(MCP_OAUTH_ENABLED=False) as harness:
        wire = harness.wire(user_agent='Claude-User/1.0')
        wire.initialize()
        listed = wire.call('prompts/list', name='prompts/list')
        got = wire.call(
            'prompts/get',
            {'name': 'check_text', 'arguments': {'text': 'Water boils at 90 °C.'}},
            name='check_text',
        )

    titles = {p['name']: p['title'] for p in listed['prompts']}
    assert titles == {
        'check_text': 'Check this text with Lenz',
        'check_last_answer': 'Check your last answer with Lenz',
    }
    assert got['messages'][0]['content']['text'] == 'Check the claims in this text with Lenz:\n\nWater boils at 90 °C.'
