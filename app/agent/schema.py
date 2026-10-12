"""The structure the editor agent must hand back, validated with pydantic."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from ..categories import CATEGORY_KEYS

CategoryKey = Literal[tuple(CATEGORY_KEYS)]  # type: ignore[valid-type]


REF_HELP = "Reference id of the piece: a candidate id like 'c12' or a search result id like 's3'."


class SourceRef(BaseModel):
    name: str = Field(description="Name of the publication or organization that published the linked page (judge by its domain), e.g. 'The Verge', 'Cloudflare'. Not the aggregator it surfaced on: use 'Hacker News' only for an HN discussion page.")
    ref: str = Field(description=REF_HELP)
    # Filled in by code from the ref after validation; never written by the model.
    url: SkipJsonSchema[str] = ""


class Story(BaseModel):
    headline: str = Field(description="Your own headline: specific, plain, under 90 characters. Not the outlet's headline copied.")
    summary: str = Field(description="2-4 sentences, under 80 words. What happened, the key specifics, and why a technical reader should care.")
    sources: list[SourceRef] = Field(description="1-3 sources, the best original reporting first.")

    @field_validator("sources")
    @classmethod
    def at_least_one_source(cls, v: list[SourceRef]) -> list[SourceRef]:
        if not v:
            raise ValueError("every story needs at least one source")
        return v


class LeadStory(BaseModel):
    category: CategoryKey  # type: ignore[valid-type]
    headline: str = Field(description="Headline for the single most important story of the period.")
    paragraphs: list[str] = Field(description="2-3 short paragraphs, each under 90 words, giving the story real depth.")
    sources: list[SourceRef]


class Section(BaseModel):
    category: CategoryKey  # type: ignore[valid-type]
    stories: list[Story]


class QuickHit(BaseModel):
    text: str = Field(description="One sentence, a complete thought, no trailing ellipsis.")
    ref: str = Field(description=REF_HELP)
    source: str
    url: SkipJsonSchema[str] = ""


class WatchItem(BaseModel):
    title: str = Field(description="The video's title, lightly cleaned up (no clickbait caps or emoji).")
    ref: str = Field(description="Candidate id of the video, e.g. 'c140'.")
    channel: str
    note: str = Field(description="1-2 sentences on what the video actually covers and who it is worth the time for, from the transcript.")
    url: SkipJsonSchema[str] = ""


class Digest(BaseModel):
    subject: str = Field(description="Email subject line, under 70 characters, naming the top one or two stories concretely.")
    preheader: str = Field(description="Inbox preview text, under 110 characters, naming other stories in the issue.")
    intro: str = Field(description="Editor's note: 2-3 sentences that orient the reader to the period. No greetings.")
    lead: LeadStory
    sections: list[Section]
    quick_hits: list[QuickHit] = Field(default_factory=list)
    watch: list[WatchItem] = Field(default_factory=list)

    def cited(self) -> list[SourceRef | QuickHit | WatchItem]:
        """Every object that carries a ref (and, once resolved, a url)."""
        items: list = list(self.lead.sources)
        for section in self.sections:
            for story in section.stories:
                items.extend(story.sources)
        items.extend(self.quick_hits)
        items.extend(self.watch)
        return items

    def headlines(self) -> list[tuple[str, str]]:
        """(url, headline) pairs for everything that ran, for de-duplicating future issues."""
        pairs = [(self.lead.sources[0].url, self.lead.headline)] if self.lead.sources else []
        for section in self.sections:
            for story in section.stories:
                for src in story.sources:
                    pairs.append((src.url, story.headline))
        pairs.extend((q.url, q.text) for q in self.quick_hits)
        pairs.extend((w.url, w.title) for w in self.watch)
        return pairs


def inline_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs resolved inline, for use as a tool input_schema."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            out = {}
            for k, v in node.items():
                if k == "properties":  # keys here are field names (one is literally "title")
                    out[k] = {name: resolve(prop) for name, prop in v.items()}
                elif k == "title" and isinstance(v, str):
                    continue  # pydantic's auto-generated titles are noise for the model
                else:
                    out[k] = resolve(v)
            return out
        if isinstance(node, list):
            return [resolve(n) for n in node]
        return node

    return resolve(schema)


def strict_schema(model: type[BaseModel]) -> dict:
    """inline_schema() made valid for strict tool use: every object closed, every property required."""

    def close(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)
        return node

    schema = close(inline_schema(model))
    for prop in schema["properties"].values():
        prop.pop("default", None)  # defaults are meaningless once every field is required
    return schema
