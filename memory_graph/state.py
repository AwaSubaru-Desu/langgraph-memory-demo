"""LangGraph 状态定义：记忆层的完整状态机字段。"""
from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class MemoryState(TypedDict, total=False):
    # ---- 输入 ----
    messages: list[dict]          # 本轮群消息（模拟飞书消息流）
    question: str                 # 可选：本轮要回答的问题

    # ---- ingest 节点产出 ----
    facts: list[dict]             # 抽取出的结构化事实（claim + evidence）
    watermark: int                # 增量拉取水位线（断点续传的关键）

    # ---- write_wiki 节点产出 ----
    written_pages: Annotated[list[str], add]  # 本轮写入/更新的 wiki 页面

    # ---- tier_check 节点产出 ----
    tier_changes: list[str]       # 冷热分层变更记录

    # ---- lint_gate 节点产出（human-in-the-loop）----
    lint_issues: list[dict]       # 防腐检查发现的问题（冲突/过期）
    lint_approved: bool           # 人工审批结果（interrupt 恢复时回填）

    # ---- retrieve_answer 节点产出 ----
    answer: str                   # 基于知识库的回答（带引用溯源）
