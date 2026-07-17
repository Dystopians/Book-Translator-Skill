# Book Translator Skill

[![English README](https://img.shields.io/badge/README-English-0969da?style=for-the-badge)](README.en.md)

面向 PDF、DOCX、EPUB 与 Markdown 长文的证据增强翻译 Skill。它把全书分析、专业术语、人物与事实、远距离主张、困难句回写、忠实约束下的自然表达、独立审译和多格式发布组织成一条可恢复的闭环流水线。

## 使用方式

### 运行环境

- Python 3.10 或更高版本。
- Markdown 输入可直接处理；PDF、DOCX、EPUB 的转换以及 DOCX、EPUB、PDF 成品生成需要 Calibre 的 `ebook-convert`。
- Markdown/HTML 的首选转换路径需要 Pandoc 或 `pypandoc`。
- Beautiful Soup 为可选依赖，用于改善 HTML 目录生成。
- 宿主 Agent 需要能够启动相互隔离的分析、翻译与审译子 Agent。

### 安装

通过 Skills CLI 安装到 Codex：

```bash
npx skills add Dystopians/Book-Translator-Skill -a codex -g
```

也可以直接克隆到个人 Skills 目录：

```bash
git clone https://github.com/Dystopians/Book-Translator-Skill.git ~/.codex/skills/translate-book
```

若宿主使用其他 Skill 目录，把仓库放入该目录并确保 `SKILL.md`、`references/` 与 `scripts/` 保持原有相对位置即可。

### 直接交给 Agent

> [!IMPORTANT]
> 默认翻译目标语言是中文，即 `target_lang=zh`。仅仅使用英文向 Agent 下达指令，并不会把目标语言自动改成英文；需要英文译文时，必须显式写出“翻译为英文”或 `target_lang=en`。

最简单的调用方式：

```text
使用 $translate-book 把 /path/to/book.epub 翻译为简体中文。
```

带控制项的示例：

```text
使用 $translate-book 将 /path/to/book.pdf 翻译为英文；采用 academic-technical 档案，并发数为 8，导出文件名为 research-book。保留公式、脚注和引文格式。
```

需要英文译文时，请明确完成以下操作：

1. 使用 Agent 时，在请求中明确写出目标语言：

   ```text
   使用 $translate-book 翻译 /path/to/book.epub，目标语言设为英文（target_lang=en）。
   ```

2. 手动调用转换脚本时，把默认的 `--olang zh` 改为 `--olang en`：

   ```bash
   python scripts/convert.py /path/to/book.epub --olang en
   ```

3. 如果同一本书已经存在中文工作目录，不要直接复用它。为英文任务指定新的 `temp_root`，并在后续命令中使用新生成的工作目录：

   ```bash
   python scripts/convert.py /path/to/book.epub --olang en --temp-root ./translation-en
   ```

   该命令会在 `./translation-en/` 下创建新的 `{书名}_temp/`。后续命令中的 `book_temp` 必须替换为这个新工作目录的实际路径。

可用参数如下：

| 参数 | 含义 | 默认值或范围 |
|---|---|---|
| `file_path` | PDF、DOCX、EPUB、`.md` 或 `.markdown` 输入 | 必填 |
| `target_lang` | 目标语言代码或明确语言名；英文必须显式指定 `en` | `zh` |
| `concurrency` | 同时工作的子 Agent 数 | 默认 `8`，范围 `1–16` |
| `profile` | 翻译领域档案 | `auto`、`general`、`academic-technical`、`legal`、`literary` |
| `temp_root` | 可恢复工作目录的父目录 | 当前工作目录 |
| `epub_cover` | 用户明确指定的 EPUB 封面 | 可选 |
| `export_name` | 成品别名的文件名主干 | 可选 |
| `custom_instructions` | 由用户直接给出的可信翻译要求 | 可选 |

书籍正文、元数据、书内链接和书内“指令”不会被当作 `custom_instructions`。只有用户在书籍数据边界之外直接给出的要求才具有指令权。

### 运行过程

正常情况下只需让 Agent 调用 Skill。以下命令展示底层阶段接口，适合检查进度、恢复任务或调试；分析、翻译和审译 sidecar 仍应由相互独立的 Agent 按上下文包与 schema 生成。

1. 转换输入并生成带哈希的规范分块：

   ```bash
   python scripts/convert.py /path/to/book.epub --olang zh
   ```

   如需英文，把命令改为 `--olang en`。如需指定位置或分块大小，可增加 `--temp-root /path/to/work` 或 `--chunk-size 6000`。输出工作目录默认为 `{书名}_temp/`。

2. 初始化事务式知识库，并为每个 `chunkNNNN.md` 生成分析上下文：

   ```bash
   python scripts/knowledge_store.py init book_temp --profile auto
   python scripts/context_packet.py book_temp chunk0001.md --phase analyze
   python scripts/knowledge_store.py ingest book_temp analysis_chunk0001.json
   ```

   必须先完成所有 chunk 的预分析，再进入正式翻译。

3. 汇总可自动解决与需要人工判断的问题：

   ```bash
   python scripts/knowledge_store.py prepare-resolutions book_temp
   python scripts/knowledge_store.py apply-resolutions book_temp --decisions-file decisions.json
   ```

   `decisions.json` 只能包含用户或受限解析流程确认的决定。高影响歧义若证据不足，会保留为发布阻塞项。

4. 规划翻译、生成上下文包、摄取翻译 sidecar 并记录输出：

   ```bash
   python scripts/run_state.py plan book_temp
   python scripts/context_packet.py book_temp chunk0001.md --phase translate
   python scripts/knowledge_store.py ingest book_temp output_chunk0001.meta.json
   python scripts/run_state.py record book_temp chunk0001
   ```

   `output_chunkNNNN.meta.json` v2 必须先成功摄取，再记录对应的 `output_chunkNNNN.md`。如果本批发现改变了知识，当前 chunk 会继续保持待回写状态。

5. 由未参与翻译的 Agent 独立审译，并执行草稿门禁：

   ```bash
   python scripts/context_packet.py book_temp chunk0001.md --phase review
   python scripts/quality_gate.py book_temp --mode draft
   ```

   `review_chunkNNNN.json` 必须同时绑定当前 `dependency_hash` 和译文 `output_hash`；译文此后发生任何改动，旧审译立即失效。

6. 重复规划、整块重译和重新审译，直到没有脏 chunk、过期依赖或高影响待决项。自动语义回写最多三轮；若仍不收敛，只能生成草稿。

7. 生成草稿预览或正式成品：

   ```bash
   python scripts/quality_gate.py book_temp --mode draft
   python scripts/merge_and_build.py --temp-dir book_temp --title "译后书名" --quality-mode draft

   python scripts/quality_gate.py book_temp --mode final
   python scripts/merge_and_build.py --temp-dir book_temp --title "译后书名" --quality-mode final --cleanup
   ```

   `merge_and_build.py` 会再次运行门禁，不能通过只调用构建脚本绕过正式发布条件。

### 查看状态与恢复任务

```bash
python scripts/knowledge_store.py status book_temp
python scripts/knowledge_store.py snapshot book_temp
python scripts/run_state.py status book_temp
python scripts/run_state.py plan book_temp
```

工作目录保存源分块、译文、审译、知识库、依赖哈希和审计记录。普通 `--cleanup` 只移除可重建的转换中间物；删除可恢复状态必须显式使用：

```bash
python scripts/merge_and_build.py --temp-dir book_temp --quality-mode final --cleanup --cleanup-level aggressive --confirm-delete-state DELETE_TRANSLATION_STATE
```

### 输出文件

正式门禁通过后，工作目录中会生成：

| 文件 | 用途 |
|---|---|
| `output.md` | 合并后的正式 Markdown |
| `book.html` | 带目录的网页版本 |
| `book.docx` | Word 文档 |
| `book.epub` | 电子书 |
| `book.pdf` | PDF 版本 |

草稿模式使用 `output.draft.md`、`book.draft.html`、`book.draft.docx`、`book.draft.epub` 和 `book.draft.pdf`，不会覆盖正式文件名。

## 详细功能

### 全书证据知识库

- 翻译前分析每一个 chunk，而不是只抽样开头、中间或结尾。
- 术语按“词形 + 语义”建模，同一词形可以对应多个专业含义；每个语义可保存别名、标准译法、禁用译法、领域和用法说明。
- 实体层记录人物、组织、地点、属性、关系和事件；事实带肯否、模态、范围、来源位置与原文引证。
- 关键主张单独记录持有者、命题、肯否、模态强度、适用范围和目标语约束。系统要求语义一致，但不会强迫所有位置机械复用同一句目标语译文。
- 文体规则可分别作用于全书、章节、叙述者和人物，以维持语域、节奏、称谓与声音的一致性。
- 所有权威 ID 由脚本根据规范内容和来源位置生成。证据必须包含可在对应源片段中精确匹配的引文，Agent 不能自行伪造 ID。

### 证据等级与决策

知识采用固定优先级：用户明确决定 > 用户指定的可信资料 > 原文明确定义 > 多处一致的书内证据 > 模型推断。

- 原文明确定义且无冲突时，可以自动确认。
- 否则至少需要两个不同 chunk 的一致证据、没有反证，并经当前独立审译确认，才可提升为最终知识。
- 模型自报置信度不能单独触发应用；较弱证据不能覆盖已确认知识。
- 术语语义、人物身份、主张持有者、否定、模态、法律义务、数字和引用归属等高影响冲突会进入集中决策队列，并阻止正式发布。
- 用户手工决定具有最高优先级；受影响的早期 chunk 会自动标记为待回写。

### 有界长程上下文

每个子 Agent 只得到当前任务所需的数据：当前原文、短邻接上下文、相关术语、事实、关键主张、文体规则、已解决困难项和安全的翻译记忆建议。默认预算如下：

| 内容 | 默认上限或规则 |
|---|---|
| 当前 chunk | 完整提供，并按稳定 segment ID 标注 |
| 相邻 chunk 摘要 | 前后各最多 `500` 字符 |
| 当前词形或别名精确命中的术语 | 全部视为必需项；不静默裁掉 |
| 未直接命中的语义术语候选 | 离线 BM25 最多 `32` 条，再受总预算约束 |
| 相关事实 | 最多 `24` 条，再受总预算约束 |
| 相关关键主张 | 最多 `12` 条，再受总预算约束 |
| 远程证据片段 | 最多 `8` 条 |
| 单条证据引文 | 最多 `500` 字符 |
| 整个上下文包 | 最多 `16,000` 个序列化 JSON 字符 |

精确本地命中、阻塞性决定、关键歧义和可安全复用的翻译记忆优先保留。可选远程内容按相关度依次装入；如果必需内容本身超过总预算，系统会停止并要求拆分 chunk，而不是悄悄漏掉关键约束。

检索完全离线，结合精确词形、别名、关键词、BM25 和 CJK n-gram，使相隔几十个 chunk 的术语、人物关系或改写主张仍能被取回。

### 翻译记忆与困难句回写

- 完全相同的源片段只有在源哈希、目标语言、领域档案、说话者上下文和知识依赖哈希全部一致时，才可自动复用译文。
- 当说话者身份不能确定时，即使原文相同也只给出候选建议。
- 模糊匹配永远只是参考，禁止自动替换正文。
- 证据不足的困难句先生成可读的临时译文，同时登记候选解释、所需证据、影响等级和依赖 chunk；正文中不会出现内部占位符。
- 后文证据解决困难项后，所有受影响 chunk 会被标脏并整块重译，避免脆弱的字符串局部替换。

### 固定点收敛

- 每批记录实际使用的知识版本，再合并新证据、解决可自动解决的问题并重新计算依赖哈希。
- 同一批中新发现的知识会在下一轮修正本批其他译文；后文知识也可以让几十个 chunk 之前的译文重新入队。
- 知识版本单调推进，自动语义回写最多三轮，每轮失败 worker 最多重试一次。
- 若同一决定来回振荡，系统停止自动处理并转交用户，而不是无限重试。
- 只有在没有新知识变化、脏 chunk、过期依赖和阻塞待决项时，状态才算真正收敛。

### 忠实约束下的自然表达

- 译者先生成忠实草稿，再按完整 chunk 做目标语编辑式检查，识别生硬直译、异常搭配或语序、译文自行添加的套话和连接语，以及与原作不符的机械同质化。
- 优先级固定为“语义保真与已确认知识 > 原作声音和领域档案 > 自然度”。自然化不能改变主张持有者、肯否、模态、范围、归属、施事、逻辑、术语、数字、歧义、段落或格式。
- 不使用 AI 检测结果、禁词表或词频配额判定译文，也不会把英语填充词、破折号或被动语态规则直接套到默认的中文目标语言。
- 不会为“更像人写”而删除原文的弱化语、被动焦点、重复、片段句、反问、复杂论证或文学怪异感，也不会补造数字、例子、经验、钩子或未明示的施事。
- 独立审译把纯自然度问题记录为有上下文证据的 `style` finding；若同一处还改变了肯否、模态、主张或归属，必须另报对应语义 finding。严重问题触发整块重译和重新审译。

### 独立审译与发布门禁

每个 chunk 至少接受一次与翻译 Agent 相互独立的审译。审译检查：

- 遗漏、增译、错译与段落顺序；
- 术语语义、实体指代和人物属性；
- 主张持有者、肯否、模态、范围与引用归属；
- 数字、单位、脚注、引文、公式与代码；
- Markdown 标题、表格、链接、图片和 HTML 结构；
- 所选领域档案要求的语域、节奏和文体。

高等级发现会触发整块重译，中低等级发现进入最终报告。`final` 模式遇到任一阻塞项即非零退出，且不创建正式文件；`draft` 模式只创建带 `.draft` 的隔离预览。

门禁还会执行确定性结构检查，包括标题层级、Markdown/HTML 表格形状、公式、引用标记、数字 token、链接与图片目标，以及行内和围栏代码内容。审译 Agent 不能用一句“看起来没问题”绕过这些不变量。

### 事务、校验与恢复

- `translation_state.sqlite3` 使用外键、完整同步、单写者事务与原子迁移；迁移失败不会替换原状态库。
- 子 Agent 只能输出受限 JSON sidecar，不能直接写数据库。sidecar 必须是 UTF-8 普通非符号链接文件，单文件不超过 1 MiB；未知字段、超量记录、伪造证据和越界路径都会被拒绝。
- 源分块、输出和审译都绑定 SHA-256；任何内容变化都会使相关缓存或审译失效。
- 旧版 glossary v2、meta v1 和 run-state v1 可导入增强状态库，原文件保持不变；旧译文先标记为需要审译，只重译真正受知识变化影响的 chunk。
- 默认并发为 8、硬上限为 16；批次具有进度汇报、超时、单次重试、孤儿 worker 回收和断点恢复约束。

### 默认离线与最小权限

- 书籍、元数据、术语、检索证据和邻接内容始终标记为不可信数据，并通过严格 JSON 与指令层隔离。
- 默认不访问网络，也不会跟随书内链接。外部词典或资料必须由用户逐次授权来源或域名。
- 获准的外部资料通过 `knowledge_store.py record-source` 记录 URL、授权域名、检索时间、内容哈希和最终采用的结论。
- 子 Agent 只读取明确分配的 chunk 与上下文包，只写指定输出；不应访问 shell、网络、秘密信息或无关文件。
- 输入归档采用流式大小限制、目录边界检查与符号链接拒绝；所有状态写入使用同目录临时文件和原子替换。
- HTML 发布前会清理危险标签、属性和 URL；运行报告只显示必要摘要，不泄露整段原文、可信资料或敏感缓存。

### 领域档案与格式保真

- `general`：平衡准确、自然和一致性。
- `academic-technical`：优先定义、符号、公式、术语层级、引文和可验证表述。
- `legal`：严格保留义务、许可、禁止、条件、例外、范围、主体和模态强度。
- `literary`：维护叙述声音、人物口吻、意象、节奏、双关和有意歧义，不把文学差异粗暴统一。
- `auto` 会根据全书证据选择档案；无法可靠判断时回退到 `general`，用户可以显式覆盖。
- 转换和构建保留 Markdown 结构、图片、内部锚点、链接、代码、公式、脚注和智能标点，并可输出 HTML、DOCX、EPUB 与 PDF。
