"""五个 LangGraph 节点：感知 → 抽取 → 存储 → 防腐（人工审批）→ 检索回答。

与实习中生产级"群聊记忆层"的对应关系：
  ingest          = 消息流感知 + ingestWatermark 增量拉取
  write_wiki      = 事实抽取 → 互链 Markdown 知识库（claim + evidence）
  tier_check      = Tier 页面冷热分层升降级
  lint_gate       = wiki lint 防腐 + 人工复核（interrupt = human-in-the-loop）
  retrieve_answer = 跨会话检索回答（引用溯源）

注：事实抽取这里用规则实现以便离线运行；生产环境替换为 LLM 结构化抽取，
节点接口和图结构保持不变。
"""
from __future__ import annotations

import re

from langgraph.types import interrupt

from .state import MemoryState
from .store import WikiStore

CN = r"A-Za-z0-9一-鿿"


# ---------- 规则抽取器（LLM 抽取的占位实现） ----------
def _extract_facts(messages: list[dict]) -> list[dict]:
    facts = []
    for m in messages:
        mid, sender, text = m["id"], m["sender"], m["text"]

        proj = re.search(rf"([{CN}]+)\s*项目正式启动，由我负责", text)
        if proj:
            project = proj.group(1)
            facts.append({"entity_type": "person", "entity": sender, "attribute": "负责",
                          "value": f"{project} 项目", "evidence": f"msg#{mid}"})
            facts.append({"entity_type": "project", "entity": project, "attribute": "状态",
                          "value": "已启动", "evidence": f"msg#{mid}"})

        dl = re.search(r"截止时间(?:改为)?\s*([0-9]+\s*月\s*[0-9]+\s*日)", text)
        if dl:
            value = re.sub(r"\s+", "", dl.group(1))
            facts.append({"entity_type": "project", "entity": "Phoenix", "attribute": "截止时间",
                          "value": value, "supersede": "作废" in text, "evidence": f"msg#{mid}"})

        addr = re.search(rf"([{CN}]{{2,10}})地址是\s*(\S+?)[，。]", text)
        if addr:
            facts.append({"entity_type": "knowledge", "entity": addr.group(1), "attribute": "地址",
                          "value": addr.group(2), "evidence": f"msg#{mid}"})

        if "使用规范" in text:
            facts.append({"entity_type": "knowledge", "entity": "知识库", "attribute": "使用规范",
                          "value": "见群文件", "evidence": f"msg#{mid}"})

        meet = re.search(rf"(\S+?)开\s*([{CN}]+)\s*项目周会，地点\s*(.+?)。?$", text)
        if meet:
            facts.append({"entity_type": "project", "entity": meet.group(2), "attribute": "周会",
                          "value": f"{meet.group(1)}，{meet.group(3)}", "evidence": f"msg#{mid}"})
    return facts


def make_nodes(store: WikiStore):
    """闭包注入 store，返回五个节点函数。"""

    def ingest(state: MemoryState) -> dict:
        wm = store.load_watermark()
        new_msgs = [m for m in state["messages"] if m["id"] > wm]
        skipped = len(state["messages"]) - len(new_msgs)
        print(f"[ingest] 水位线={wm}，本轮 {len(state['messages'])} 条消息，"
              f"跳过已消费 {skipped} 条，新消费 {len(new_msgs)} 条")
        facts = _extract_facts(new_msgs)
        print(f"[ingest] 抽取事实 {len(facts)} 条")
        new_wm = max((m["id"] for m in new_msgs), default=wm)
        return {"facts": facts, "watermark": new_wm}

    def write_wiki(state: MemoryState) -> dict:
        pages = []
        for f in state.get("facts", []):
            claim = f"{f['attribute']}：{f['value']}"
            rel = store.append_claim(f["entity_type"], f["entity"], claim, f["evidence"])
            pages.append(rel)
            print(f"[write_wiki] {rel} += 「{claim}」")
        if state.get("watermark"):
            store.save_watermark(state["watermark"])
        return {"written_pages": pages}

    def tier_check(state: MemoryState) -> dict:
        changes = []
        for f in state.get("facts", []):
            n = store.count_claims(f["entity_type"], f["entity"])
            target = "hot" if n >= 2 else "cold"
            if store.set_tier(f["entity_type"], f["entity"], target):
                changes.append(f"{f['entity']} → {target}（{n} 条事实）")
                print(f"[tier_check] {changes[-1]}")
        store.rebuild_index()
        if not changes:
            print("[tier_check] 无分层变更")
        return {"tier_changes": changes}

    def lint_gate(state: MemoryState) -> dict:
        """防腐检查：同一实体同一属性出现多个不同取值 → 挂起等人工审批。"""
        conflicts = {}
        for f in state.get("facts", []):
            key = (f["entity"], f["attribute"])
            conflicts.setdefault(key, set()).add(f["value"])
        issues = [
            {"entity": k[0], "attribute": k[1], "values": sorted(v)}
            for k, v in conflicts.items() if len(v) > 1
        ]
        if issues and not state.get("lint_approved"):
            # 挂起等人工审批；冲突详情通过 interrupt payload 抛给调用方（看板/审核员）
            decision = interrupt({"type": "lint_review", "issues": issues})
            if decision:  # 审批通过：以最新口径为准，在页面留痕
                for i in issues:
                    store.append_claim(
                        "project", i["entity"],
                        f"[lint] 「{i['attribute']}」存在多口径 {i['values']}，人工审批以最新值为准，旧口径作废",
                        "lint_gate 人工审批",
                    )
                print("[lint_gate] 人工审批通过：以最新口径为准，旧口径已在页面留痕作废")
            return {"lint_approved": bool(decision), "lint_issues": issues}

        if issues and state.get("lint_approved"):
            print("[lint_gate] 冲突已审批过，直接放行")
        else:
            print("[lint_gate] 未发现冲突，直接放行")
        return {"lint_issues": issues}

    def retrieve_answer(state: MemoryState) -> dict:
        q = state.get("question", "")
        if not q:
            return {"answer": ""}
        print(f"[retrieve_answer] 问题：{q}")
        # 取问题中的实体关键词做全库检索（生产环境：向量 + 图谱混合检索）
        keywords = re.findall(rf"[{CN}]{{2,}}", q)
        stopwords = {"什么", "时候", "项目", "负责", "谁"}
        lines = []
        for kw in dict.fromkeys(keywords):
            if kw in stopwords:
                continue
            for page, line in store.search(kw):
                if line.startswith("- "):  # 只引用事实行（claim + evidence）
                    lines.append(f"  - {line[2:]}　—— 来源：{page}")
        answer = "检索到以下相关事实：\n" + "\n".join(lines) if lines else "知识库中没有相关记录。"
        return {"answer": answer}

    return {
        "ingest": ingest,
        "write_wiki": write_wiki,
        "tier_check": tier_check,
        "lint_gate": lint_gate,
        "retrieve_answer": retrieve_answer,
    }
