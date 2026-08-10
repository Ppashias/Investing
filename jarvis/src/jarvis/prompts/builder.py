"""Assembles the system prompt for a request."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jarvis.prompts import identity
from jarvis.prompts.blocks import BlockOrder, PromptBlock, SystemPrompt

if TYPE_CHECKING:
    from jarvis.context.manager import ContextBundle


class SystemPromptBuilder:
    """Builds the prompt from static identity blocks plus per-request context.

    Static blocks come first and are byte-identical across requests, so they
    form a stable cacheable prefix; volatile context is appended after.
    """

    def build(self, context: "ContextBundle | None" = None) -> SystemPrompt:
        prompt = SystemPrompt()

        prompt.add(
            PromptBlock(
                id="identity",
                title="Who you are",
                content=identity.CORE_IDENTITY,
                order=BlockOrder.IDENTITY,
            )
        )
        prompt.add(
            PromptBlock(
                id="behavior",
                title="How to respond",
                content=identity.BEHAVIOR_RULES,
                order=BlockOrder.BEHAVIOR,
            )
        )
        prompt.add(
            PromptBlock(
                id="capabilities",
                title="What you can and cannot do",
                content=identity.CAPABILITIES_IMPLEMENTED,
                order=BlockOrder.CAPABILITIES,
            )
        )
        prompt.add(
            PromptBlock(
                id="tool_guidance",
                title="Using tools",
                content=identity.TOOL_GUIDANCE,
                order=BlockOrder.TOOL_GUIDANCE,
            )
        )
        prompt.add(
            PromptBlock(
                id="security",
                title="Security rules",
                content=identity.SECURITY_RULES,
                order=BlockOrder.SECURITY,
            )
        )

        if context is not None:
            self._add_dynamic(prompt, context)

        return prompt

    def _add_dynamic(self, prompt: SystemPrompt, context: "ContextBundle") -> None:
        if context.user_context:
            prompt.add(
                PromptBlock(
                    id="user_context",
                    title="About the user",
                    content=context.user_context,
                    order=BlockOrder.USER_CONTEXT,
                    cacheable=False,
                )
            )
        if context.project_context:
            prompt.add(
                PromptBlock(
                    id="project_context",
                    title="Current project",
                    content=context.project_context,
                    order=BlockOrder.PROJECT_CONTEXT,
                    cacheable=False,
                )
            )
        if context.task_context:
            prompt.add(
                PromptBlock(
                    id="task_context",
                    title="Open tasks",
                    content=context.task_context,
                    order=BlockOrder.TASK_CONTEXT,
                    cacheable=False,
                )
            )
        if context.memory_context:
            prompt.add(
                PromptBlock(
                    id="memory",
                    title="Relevant memory",
                    content=context.memory_context,
                    order=BlockOrder.MEMORY,
                    cacheable=False,
                )
            )

        prompt.add(
            PromptBlock(
                id="runtime",
                title="Runtime context",
                content=self._runtime_block(context),
                order=BlockOrder.RUNTIME_CONTEXT,
                cacheable=False,
            )
        )

    @staticmethod
    def _runtime_block(context: "ContextBundle") -> str:
        now = datetime.now(timezone.utc)
        lines = [
            f"Current UTC time: {now.isoformat(timespec='seconds')}.",
            "This timestamp is the moment the request started. For anything "
            "time-sensitive, or for a timezone other than UTC, call "
            "get_current_time rather than deriving from this value.",
        ]
        if context.conversation_id:
            lines.append(f"Conversation id: {context.conversation_id}.")
        if context.truncated:
            lines.append(
                "Earlier turns in this conversation have been dropped to fit the "
                "context budget. If the user refers to something you cannot see, "
                "say so rather than guessing."
            )
        return "\n".join(lines)
