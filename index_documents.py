"""知识库索引脚本：递归扫描 data/library（只读「公共书架」）下的文档并向量化入库

变更说明：
- 使用 Path.rglob 递归扫描书架及其子目录（此前仅扫描根目录，子目录文档无法入库）；
- 索引根＝只读「公共书架」data/library（P7 批次 5b 从 data/files 改指；后者是"无目标
  上下文时的私有写入根"，模型随手写的草稿会被递归索引进共享知识库）；
- **书架是文档类语料的唯一来源，但 rag_search 并不只命中书架**：向量集合
  personal_knowledge 由两半共用——文档行（本脚本写入，带 source）与笔记行
  （rag/note_indexer.py 写入，带 note_id、没有 source），而 rag/retriever.py 的查询
  不带 where，两半都会被命中；
- **向量库对账（文档 + 笔记）在每次运行的最前面**：文档侧删 `source` 已不在书架上的行
  （`prune_orphan_sources`），笔记侧删 `note_id` 已不在 `notes` 表里的行
  （`rag.note_indexer.live_note_ids` + `prune_orphan_notes`）。对账发生在"书架为空就提前
  返回"**之前**，否则清空书架后旧向量会原样留着；两侧都被顶部那道 `files_dir` 安全闸挡着。
  笔记侧原先只有"改/删笔记"时才触发删除、且失败只 print，孤儿向量因此无声积累（本机实测
  积了 62 条，会被 rag_search 当笔记返回）；这里给它一个统一入口，与文档侧同规；
- rag_search 命中的两半都在对账范围内：文档行（本脚本写入，带 `source`）与笔记行
  （`rag/note_indexer.py` 写入，带 `note_id`），而 `rag/retriever.py` 的查询不带 where；
- 逐文件入库是**先删后加**（先按 source 删本文件的旧块，再写新块）：文档 ID 为
  md5(相对路径)[:12] + "_" + 块序号，纯 upsert 在"新版块数少于旧版"时会留下序号更大的
  陈旧块，它们删不掉却仍会被检索到。代价如实记：删与加之间若中断，该文件的向量会缺到
  下次成功运行——比静默留下陈旧块好，故本步**不是**完全幂等；
- 单个文件读取/嵌入失败只记录错误并继续，不中断整体索引流程。
"""
import hashlib
from pathlib import Path

from rag.document_loader import load_and_split
from rag.embedding import embed_documents
from rag.note_indexer import live_note_ids, prune_orphan_notes
from rag.vector_store import add_documents, delete_documents

# 支持的文档扩展名（与 rag/document_loader.load_and_split 的白名单保持一致）
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown"}

# 知识库根目录＝只读「公共书架」（P7 批次 5b：原为 data/files，那是私有写入根）
files_dir = Path("data/library")


def _file_key(rel_path: str) -> str:
    """由相对路径生成稳定的短哈希键（同一文件重跑时 ID 不变）"""
    return hashlib.md5(rel_path.encode("utf-8")).hexdigest()[:12]


def iter_supported_files(root: Path) -> list:
    """递归枚举 root 下所有受支持的文档文件（按路径排序，保证处理顺序稳定）"""
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def prune_orphan_sources(files) -> int:
    """删除向量库里 source 已不在当前书架文件集合中的文档行，返回实际删除条数

    Args:
        files: 当前书架上的文件（Path 列表，元素须位于 `files_dir` 之下），
               通常就是 `iter_supported_files(files_dir)` 的结果

    只碰「带 source 且 source ∉ 现存文件集合」的那些行：逐个
    `delete_documents(where={"source": src})`。**绝不**传 None、不做无条件删除——
    同一个集合里还有 rag/note_indexer.py 写入的笔记行（只有 note_id、没有 source），
    把 None 交给 where 会把它们整批选中（见 `rag.vector_store.source_counts` 的说明）。

    删前打印将删清单（每个 source + 条数），删后按 source_counts 的实际变化报总数；
    预期与实际不符时额外告警——对账这件事不允许静默。
    """
    from rag.vector_store import source_counts, delete_documents

    counts = source_counts()
    if not counts:
        print("向量库对账：集合里没有带 source 的文档行，无需对账")
        return 0

    alive = {f.relative_to(files_dir).as_posix() for f in files}
    orphans = {src: n for src, n in counts.items() if src not in alive}
    if not orphans:
        print(f"向量库对账：{len(counts)} 个 source 都还在书架上，无需删除")
        return 0

    before = sum(counts.values())
    print(f"向量库对账：以下 {len(orphans)} 个 source 已不在书架上，将删除其全部向量：")
    for src, n in sorted(orphans.items()):
        print(f"  - {src}（{n} 条）")
    for src in sorted(orphans):
        delete_documents(where={"source": src})

    removed = before - sum(source_counts().values())
    expected = sum(orphans.values())
    if removed != expected:
        print(f"⚠️ 向量库对账：预期删除 {expected} 条，实际减少 {removed} 条"
              f"（可能有并发写入，或删除未生效）")
    print(f"向量库对账：已删除 {removed} 条文档向量（涉及 {len(orphans)} 个 source）")
    return removed


def main() -> None:
    """执行全量索引：向量库对账（文档 + 笔记）→ 逐文件加载 → 切分 → 嵌入 → 入库"""
    # 安全闸（P7 批次 6-①）：files_dir 是相对当前工作目录的路径，在错误的目录下运行会把
    # "书架是空的"当成事实——若照常对账，就会把整个文档语料当成孤儿删掉。故书架目录不存在
    # 时既不索引也不对账（**两侧都不对账**），只告警。
    if not files_dir.is_dir():
        print(f"⚠️ 书架目录 {files_dir} 不存在（可能是工作目录不对），跳过索引与向量库对账。")
        return

    files = iter_supported_files(files_dir)

    # 对账必须在下面 `if not files` 之前：书架被清空后脚本若提前 return，
    # 旧向量会原样留着，"书架是文档语料唯一来源"就永远不成立。
    prune_orphan_sources(files)

    # 笔记侧对账（P7 批次 7-⑤）：同一个集合的另一半由 rag/note_indexer.py 写入，
    # 它的孤儿（笔记已删、向量还在）同样会经 rag_search 返回。live_note_ids() 返回 None
    # ＝读不到笔记库 ⇒ 一条都不删（None 与空集必须分开：空集＝确实没有笔记，才该全删）。
    live_ids = live_note_ids()
    if live_ids is None:
        print("⚠️ 读不到笔记库，跳过笔记侧对账（不删任何笔记向量）。")
    else:
        prune_orphan_notes(live_ids)

    if not files:
        print(f"未发现可索引的文档（{files_dir} 下没有 .txt/.md/.markdown 文件）")
        return

    print(f"共发现 {len(files)} 个待索引文档（含子目录）")
    ok_count, skip_count, fail_count = 0, 0, 0
    for file in files:
        # 以相对路径作为文件唯一标识：子目录同名文件不会冲突
        rel_path = file.relative_to(files_dir).as_posix()
        try:
            chunks = load_and_split(str(file))
            if not chunks:
                # 无有效内容：跳过。这里**不**删旧块——文件仍在书架上，对账不会碰它，
                # 所以"内容改成空白"的文件会留着上一次的向量（既有行为，本批不改）。
                print(f"已跳过（无有效内容）：{rel_path}")
                skip_count += 1
                continue
            embeddings = embed_documents(chunks)
            metadatas = [{"source": rel_path} for _ in chunks]
            # ID 前缀取相对路径哈希 + 块序号：同一文件重跑时同序号的块互相覆盖
            ids = [f"{_file_key(rel_path)}_{i}" for i in range(len(chunks))]
            # 先删后加（P7 批次 6-①）：纯 upsert 在"新版块数少于旧版"时，序号更大的旧块
            # 永远覆盖不到、也删不掉，会继续被检索到。这里先按 source 删掉本文件的全部旧块
            # 再写新块，使集合里该 source 的条数恰好等于本次块数。放在嵌入之后删，是为了把
            # "删了但没加回来"的窗口压到最短。代价：该窗口内中断会让这个文件的向量缺到下次
            # 成功运行——比静默留下陈旧块好，故本步不是完全幂等。
            delete_documents(where={"source": rel_path})
            # 传入 bge-m3 预计算的向量，保证入库与查询使用同一向量空间
            add_documents(chunks, metadatas, ids, embeddings=embeddings)
            print(f"已索引：{rel_path}（{len(chunks)}块）")
            ok_count += 1
        except Exception as e:
            # 单个文件失败不影响其他文件的索引
            print(f"❌ 索引失败：{rel_path}（{e}）")
            fail_count += 1

    print(f"索引完成：成功 {ok_count} 个，跳过 {skip_count} 个，失败 {fail_count} 个")


if __name__ == "__main__":
    main()
