"""WikiStore：互链 Markdown 知识库的持久化层。

对应实习中"群聊记忆层"的存储设计：
- 三类实体页面：people / projects / knowledge
- 每条事实以 claim + evidence（证据溯源）形式追加进页面
- 页面带 tier（hot/cold 冷热分层）front matter
- watermark 文件记录已消费的消息位置，支撑增量拉取
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


class WikiStore:
    def __init__(self, root: str | Path = "data/wiki"):
        self.root = Path(root)
        for sub in ("people", "projects", "knowledge"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.watermark_file = self.root / ".watermark"
        self.index_file = self.root / "INDEX.md"

    # ---------- watermark（增量拉取 / 断点续传） ----------
    def load_watermark(self) -> int:
        if self.watermark_file.exists():
            return int(self.watermark_file.read_text().strip() or 0)
        return 0

    def save_watermark(self, wm: int) -> None:
        self.watermark_file.write_text(str(wm))

    # ---------- 页面读写 ----------
    def _page_path(self, entity_type: str, name: str) -> Path:
        sub = {"person": "people", "project": "projects", "knowledge": "knowledge"}[entity_type]
        safe = re.sub(r"[^\w一-鿿-]", "_", name)
        return self.root / sub / f"{safe}.md"

    def read_page(self, entity_type: str, name: str) -> str | None:
        p = self._page_path(entity_type, name)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def append_claim(self, entity_type: str, name: str, claim: str, evidence: str) -> str:
        """向实体页面追加一条带证据的事实，返回页面相对路径。"""
        p = self._page_path(entity_type, name)
        if p.exists():
            body = p.read_text(encoding="utf-8")
        else:
            body = (
                "---\n"
                f"type: {entity_type}\n"
                f"name: {name}\n"
                "tier: cold\n"
                "---\n\n"
                f"# {name}\n\n## 事实记录\n"
            )
        line = f"- {claim}（证据：{evidence}）\n"
        body = body.rstrip("\n") + "\n" + line
        p.write_text(body, encoding="utf-8")
        return str(p.relative_to(self.root))

    def set_tier(self, entity_type: str, name: str, tier: str) -> bool:
        """更新页面冷热分层，返回 tier 是否发生变化。"""
        p = self._page_path(entity_type, name)
        if not p.exists():
            return False
        body = p.read_text(encoding="utf-8")
        m = FRONT_RE.match(body)
        if not m:
            return False
        front = m.group(1)
        old = re.search(r"tier:\s*(\w+)", front)
        if old and old.group(1) == tier:
            return False
        front = re.sub(r"tier:\s*\w+", f"tier: {tier}", front)
        p.write_text(FRONT_RE.sub(f"---\n{front}\n---\n", body), encoding="utf-8")
        return True

    def get_tier(self, entity_type: str, name: str) -> str | None:
        body = self.read_page(entity_type, name)
        if not body:
            return None
        m = FRONT_RE.match(body)
        if not m:
            return None
        t = re.search(r"tier:\s*(\w+)", m.group(1))
        return t.group(1) if t else None

    def count_claims(self, entity_type: str, name: str) -> int:
        body = self.read_page(entity_type, name) or ""
        return len(re.findall(r"^- ", body, re.M))

    def search(self, keyword: str) -> list[tuple[str, str]]:
        """全库关键词检索，返回 (页面, 命中行)。生产环境替换为向量+图谱混合检索。

        两级匹配：关键词命中页面名 → 返回该页全部事实行（图谱式整页召回）；
        否则退化为事实行内容匹配。
        """
        hits = []
        for md in sorted(self.root.rglob("*.md")):
            if md.name == "INDEX.md":
                continue
            body = md.read_text(encoding="utf-8")
            claims = [l.strip() for l in body.splitlines() if l.startswith("- ")]
            if keyword in md.stem:
                hits.extend((str(md.relative_to(self.root)), l) for l in claims)
            else:
                hits.extend(
                    (str(md.relative_to(self.root)), l) for l in claims if keyword in l
                )
        return hits

    def rebuild_index(self) -> None:
        """重建全库索引（防腐机制的一部分：索引与页面同步）。"""
        lines = ["# 知识库索引\n"]
        for sub, label in (("people", "人"), ("projects", "项目"), ("knowledge", "知识")):
            lines.append(f"\n## {label}\n")
            for md in sorted((self.root / sub).glob("*.md")):
                body = md.read_text(encoding="utf-8")
                tier = re.search(r"tier:\s*(\w+)", body)
                n = len(re.findall(r"^- ", body, re.M))
                lines.append(f"- [{md.stem}]({sub}/{md.name}) — tier: {tier.group(1) if tier else '?'}，{n} 条事实\n")
        self.index_file.write_text("".join(lines), encoding="utf-8")

    def dump_json(self) -> str:
        """导出全库概要（演示用）。"""
        out = {}
        for sub in ("people", "projects", "knowledge"):
            for md in sorted((self.root / sub).glob("*.md")):
                out[str(md.relative_to(self.root))] = {
                    "tier": self.get_tier(
                        {"people": "person", "projects": "project", "knowledge": "knowledge"}[sub],
                        md.stem,
                    ),
                    "claims": len(re.findall(r"^- ", md.read_text(encoding="utf-8"), re.M)),
                }
        return json.dumps(out, ensure_ascii=False, indent=2)
