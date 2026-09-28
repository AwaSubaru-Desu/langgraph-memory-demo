"""入口：构建图 → 跑两轮会话，演示跨会话记忆与人工审批中断。

运行：python main.py
"""
from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from memory_graph.nodes import make_nodes
from memory_graph.sample_data import ROUND1_MESSAGES, ROUND2_MESSAGES, ROUND2_QUESTION
from memory_graph.state import MemoryState
from memory_graph.store import WikiStore

DB = "data/checkpoints.db"


def build_graph(store: WikiStore, checkpointer):
    nodes = make_nodes(store)
    g = StateGraph(MemoryState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    # 感知 → 抽取 → 存储 → 分层 → 防腐（人工审批）→ 检索回答
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "write_wiki")
    g.add_edge("write_wiki", "tier_check")
    g.add_edge("tier_check", "lint_gate")
    g.add_edge("lint_gate", "retrieve_answer")
    g.add_edge("retrieve_answer", END)
    return g.compile(checkpointer=checkpointer)


def main() -> None:
    store = WikiStore("data/wiki")
    Path("data").mkdir(exist_ok=True)
    config = {"configurable": {"thread_id": "demo-session"}}

    with SqliteSaver.from_conn_string(DB) as saver:
        graph = build_graph(store, saver)

        print("=" * 60)
        print("第一轮：群消息 ingest（含一次口径变更冲突）")
        print("=" * 60)
        result = graph.invoke({"messages": ROUND1_MESSAGES}, config)

        # lint_gate 触发 interrupt：模拟人工审批（生产环境 = 审核员在看板点"通过"）
        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            print("\n>>> 图在 lint_gate 挂起（interrupt = human-in-the-loop），等待人工审批：")
            for i in payload["issues"]:
                print(f"    冲突：{i['entity']} 的「{i['attribute']}」存在多个取值：{i['values']}")
            print(">>> 人工决策：以最新口径为准（resume=True）\n")
            result = graph.invoke(Command(resume=True), config)

        print("\n" + "=" * 60)
        print("第二轮：新消息 + 跨会话提问（同一个 thread_id，同一份知识库）")
        print("=" * 60)
        result = graph.invoke(
            {"messages": ROUND2_MESSAGES, "question": ROUND2_QUESTION},
            config,
        )
        print("\n最终回答：")
        print(result["answer"])

        print("\n" + "=" * 60)
        print("知识库概要（tier / 事实条数）")
        print("=" * 60)
        print(store.dump_json())


if __name__ == "__main__":
    main()
