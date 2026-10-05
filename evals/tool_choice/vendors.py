"""Ask one vendor's model which tool it would call. Nothing is executed.

Mode A: the manifest is declared as ordinary client-side tools, so no server,
no public URL, no Lenz credits, and it works on a branch before a deploy.

WHERE THE INSTRUCTIONS GO, and why it is a choice rather than a mirror: checked
against both vendors' current docs on 2026-09-18, **neither API connector
documents sending an MCP server's `instructions` to the model at all.**
Anthropic's connector says its scope outright ("only tool calls are currently
supported") and points callers at their own system prompt instead; OpenAI's
remote-MCP tool puts an `mcp_list_tools` item in context holding tool
definitions, with no field for server instructions and no `server_description`.

So Mode A places them in the SYSTEM PROMPT. That is a deliberate best case: it
approximates the APPS, which demonstrably do respond to this wording (trigger
sentences in the instructions changed activation in Claude and ChatGPT — a
measurement of the apps, not a documented behaviour), and it over-states what an
API connector delivers, which is nothing. `--no-instructions` runs the same
cases without them, and the delta is the honest measure of how much of the
connector's activation behaviour rests on a channel a whole class of client never receives.

"Not documented" is not "not delivered": read the paragraph above as the docs'
claim, not as a measurement. A canary (a nonsense token in the probe server's
instructions, asked for through each channel) is how to measure it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from evals.tool_choice.manifest import Manifest

# The models the APPS most plausibly run, not the dearest available
# (2026-09-18): a result measured on a flagship would flatter wording that a
# mid-tier model in a real product may not follow. Overridable with --model, and
# the exact id is written into every result file — nobody should read a number
# here as "Claude" or "ChatGPT" in general.
#
# Refreshed 2026-10-05 to the newest of each line: Sonnet 5.5 (was Sonnet 5) and `gpt-6-sol` (was
# `gpt-5.6-terra`; GPT-6 has no Terra tier). Each is an EXACT id on purpose: a
# bare family alias may route to a different, dearer tier, and a result must
# name the model it ran. `--check-fresh` accepts only a result measured on
# THESE models, so changing a default makes every earlier result stale.
DEFAULT_MODELS = {'anthropic': 'claude-sonnet-5-5', 'openai': 'gpt-6-sol'}

# A tool the real apps put beside the connector's: a general web search. The
# connector is normally the ONLY tool the model sees in this eval, which flatters
# every "must not fire" case (nothing else to reach for) and understates every
# under-trigger case (nothing competes). `--distractor` adds this one; a call to
# it is never scored as a Lenz call (see `scoring.judge`). Report-only.
DISTRACTOR_NAME = 'web_search'
DISTRACTOR_TOOL = {
    'name': DISTRACTOR_NAME,
    'description': (
        'Search the web and return the top results with short snippets. Use it for current events '
        'and for anything you do not already know.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {'query': {'type': 'string', 'description': 'The search query.'}},
        'required': ['query'],
    },
}

MAX_TOKENS = 1024


@dataclass(frozen=True)
class Call:
    """One tool call a model asked for, as the eval reads it."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """What one call cost, as the vendor reports it.

    `cached` is the part of the input the vendor served from its prompt cache.
    The manifest — 2,209 characters of instructions plus seven long tool
    descriptions — is identical for every case in an arm, so it should be cached
    from the second call onward. If `cached` stays at zero the run is paying full
    price for the same preamble 40 times, which is worth seeing rather than
    inferring from the bill.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # Anthropic bills a cache WRITE at 1.25x input and reports it apart from
    # both `input_tokens` and the cached reads. Discarding it would make every
    # spend figure short by the manifest's cost once per arm.
    # OpenAI caches without a write charge, so it stays 0 there.
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Turn:
    """What a model answered: the calls it wanted, and any text beside them."""

    calls: list[Call]
    text: str
    usage: Usage = Usage()

    @property
    def names(self) -> list[str]:
        return [call.name for call in self.calls]


def _system_prompt(manifest: Manifest, with_instructions: bool) -> str:
    if not with_instructions:
        return ''
    return manifest.instructions


def _tool_text(result: dict[str, Any]) -> str:
    """A tool result as a client shows it to its model: the JSON the tool returned."""
    return json.dumps(result, ensure_ascii=False)


def _anthropic_messages(history: tuple[Any, ...], prompt: str) -> list[dict[str, Any]]:
    """History as Anthropic messages: a `ToolExchange` is a real `tool_use` block and its `tool_result`."""
    from evals.tool_choice.cases import ToolExchange

    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(history):
        if isinstance(turn, ToolExchange):
            call_id = f'toolu_eval{index:02d}'
            turns.append(
                {
                    'role': 'assistant',
                    'content': [{'type': 'tool_use', 'id': call_id, 'name': turn.name, 'input': turn.arguments}],
                }
            )
            turns.append(
                {
                    'role': 'user',
                    'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': _tool_text(turn.result)}],
                }
            )
        else:
            role, text = turn
            turns.append({'role': role, 'content': text})
    if prompt:
        turns.append({'role': 'user', 'content': prompt})
    return turns


def _openai_input(history: tuple[Any, ...], prompt: str) -> list[dict[str, Any]]:
    """History as Responses API input items: a `ToolExchange` is a `function_call` and its output."""
    from evals.tool_choice.cases import ToolExchange

    items: list[dict[str, Any]] = []
    for index, turn in enumerate(history):
        if isinstance(turn, ToolExchange):
            call_id = f'call_eval{index:02d}'
            items.append(
                {
                    'type': 'function_call',
                    'call_id': call_id,
                    'name': turn.name,
                    'arguments': json.dumps(turn.arguments, ensure_ascii=False),
                }
            )
            items.append({'type': 'function_call_output', 'call_id': call_id, 'output': _tool_text(turn.result)})
        else:
            role, text = turn
            items.append({'role': role, 'content': text})
    if prompt:
        items.append({'role': 'user', 'content': prompt})
    return items


def ask_anthropic(
    manifest: Manifest,
    history: tuple[Any, ...],
    prompt: str,
    *,
    model: str,
    with_instructions: bool,
    distractor: bool = False,
) -> Turn:
    import anthropic

    # No SDK retries: the default of two means one planned call can be sent
    # three times, each possibly billed, none of them counted against
    # --max-calls. A failed call should fail its attempt where it can be seen.
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'], max_retries=0)
    tools = [
        {
            'name': tool['name'],
            'description': tool['description'],
            'input_schema': tool['input_schema'],
        }
        for tool in [*manifest.tools, *([DISTRACTOR_TOOL] if distractor else [])]
    ]
    # Prompt caching: the manifest is identical across every case in an arm, so
    # the breakpoint goes on the LAST tool — a cache_control marker caches
    # everything above it, which here is the system prompt and all seven tools.
    tools[-1] = {**tools[-1], 'cache_control': {'type': 'ephemeral'}}
    kwargs: dict[str, Any] = {
        'model': model,
        'max_tokens': MAX_TOKENS,
        'messages': _anthropic_messages(history, prompt),
        'tools': tools,
    }
    system = _system_prompt(manifest, with_instructions)
    if system:
        kwargs['system'] = [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}]
    message = client.messages.create(**kwargs)

    calls: list[Call] = []
    text: list[str] = []
    for block in message.content:
        if block.type == 'tool_use':
            calls.append(Call(name=block.name, arguments=dict(block.input or {})))
        elif block.type == 'text':
            text.append(block.text)
    reported = getattr(message, 'usage', None)
    usage = Usage(
        input_tokens=getattr(reported, 'input_tokens', 0) or 0,
        output_tokens=getattr(reported, 'output_tokens', 0) or 0,
        # Anthropic reports the two halves separately: what was written to the
        # cache this call, and what was read from it.
        cached_tokens=(getattr(reported, 'cache_read_input_tokens', 0) or 0),
        cache_write_tokens=(getattr(reported, 'cache_creation_input_tokens', 0) or 0),
    )
    return Turn(calls=calls, text='\n'.join(text), usage=usage)


def ask_openai(
    manifest: Manifest,
    history: tuple[Any, ...],
    prompt: str,
    *,
    model: str,
    with_instructions: bool,
    distractor: bool = False,
) -> Turn:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], max_retries=0)  # see ask_anthropic
    kwargs: dict[str, Any] = {
        'model': model,
        'input': _openai_input(history, prompt),
        'tools': [
            {
                'type': 'function',
                'name': tool['name'],
                'description': tool['description'],
                'parameters': tool['input_schema'],
            }
            for tool in [*manifest.tools, *([DISTRACTOR_TOOL] if distractor else [])]
        ],
    }
    system = _system_prompt(manifest, with_instructions)
    if system:
        kwargs['instructions'] = system
    response = client.responses.create(**kwargs)

    calls: list[Call] = []
    text: list[str] = []
    for item in response.output:
        kind = getattr(item, 'type', '')
        if kind == 'function_call':
            try:
                arguments = json.loads(item.arguments or '{}')
            except json.JSONDecodeError:
                arguments = {}
            calls.append(Call(name=item.name, arguments=arguments if isinstance(arguments, dict) else {}))
        elif kind == 'message':
            for part in getattr(item, 'content', []) or []:
                if getattr(part, 'type', '') == 'output_text':
                    text.append(part.text)
    reported = getattr(response, 'usage', None)
    details = getattr(reported, 'input_tokens_details', None)
    usage = Usage(
        input_tokens=getattr(reported, 'input_tokens', 0) or 0,
        output_tokens=getattr(reported, 'output_tokens', 0) or 0,
        # OpenAI caches automatically and reports the hit here; nothing to ask for.
        cached_tokens=(getattr(details, 'cached_tokens', 0) or 0),
    )
    return Turn(calls=calls, text='\n'.join(text), usage=usage)


ASK = {'anthropic': ask_anthropic, 'openai': ask_openai}
