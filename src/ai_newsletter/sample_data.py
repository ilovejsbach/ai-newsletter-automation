from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Article


def sample_articles() -> list[Article]:
    now = datetime.now(timezone.utc)
    return [
        Article(
            id="sample-openai-agent",
            source_id="sample",
            source_name="Sample Source",
            title="OpenAI introduces new agentic workflow tools for enterprise AI",
            url="https://example.com/openai-agentic-workflow",
            published_at=now - timedelta(days=1),
            summary="A new set of tools focuses on agentic workflows, tool use, and safer enterprise deployment.",
            tags=["agent", "enterprise", "llm"],
            source_weight=1.2,
        ),
        Article(
            id="sample-hf-model",
            source_id="sample",
            source_name="Hugging Face",
            title="new-lab/fast-reasoning-model",
            url="https://huggingface.co/new-lab/fast-reasoning-model",
            published_at=now - timedelta(days=2),
            summary="text-generation, reasoning, open model",
            tags=["text-generation", "reasoning", "open model"],
            metrics={"downloads": 42000, "likes": 780},
            source_weight=1.0,
        ),
        Article(
            id="sample-github",
            source_id="sample",
            source_name="GitHub",
            title="acme-ai/browser-agent",
            url="https://github.com/acme-ai/browser-agent",
            published_at=now - timedelta(days=3),
            summary="An open-source browser automation agent for LLM-powered workflows.",
            tags=["agents", "llm", "automation"],
            metrics={"stars": 8200, "forks": 610},
            source_weight=1.0,
        ),
    ]
