> 本 README 是 clean-writer-cn 的实际使用案例, 由 GPT-5.6 Sol 在最高推理强度下为本仓库撰写.

# clean-writer-cn

clean-writer-cn 是一个供 Codex 使用的简体中文写作 skill. 它可以写新文章, 重写已有内容, 或检查中文表达. 它适合技术报告和技术说明, 也能处理博客, 代码注释或局部段落.

写作前, 它先弄清文章写给谁, 读者看完要知道什么或做什么. 然后才选择材料和详细程度. 源码, 命令和计算只用来核对事实. 它们能说明流程, 条件或取舍时, 这些内容会写进正文; 文件名和变量名通常不会. 文章会保留必要的技术内容, 但不会变成实现清单.

## 安装

运行规则脚本需要 `uv`. 将仓库克隆到 Codex 的个人 skills 目录:

```powershell
git clone https://github.com/x380kkm/clean-writer-cn.git ~/.codex/skills/clean-writer-cn
```

安装后, 在写作任务中调用 `$clean-writer-cn`.

## 使用

新写文章时, 提供主题, 读者需要完成的事和可用材料:

```text
使用 $clean-writer-cn, 根据这些材料写一篇面向普通开发者的技术说明. 写清完整流程和主要取舍, 不展开代码名称和项目路径.
```

修改已有文章时, 直接说明需要保留和改变的内容:

```text
使用 $clean-writer-cn 重写这篇文章. 保留事实, 条件和判断强度, 删除翻译腔和生硬表达, 不缩减技术流程.
```

审核文风时, 可以要求直接交付修订稿:

```text
使用 $clean-writer-cn 审核这篇中文说明. 找出行话, 空洞概括和不自然的句式, 修正后给出完整正文.
```

## 写作方法

写作由主笔完成. 它决定文章写给谁, 采用哪些材料, 如何安排段落, 最后怎样表述. 需要核对技术事实时, 研究只说明实际发生了什么, 在什么条件下发生, 顺序如何, 哪些地方还不能确认. 研究不替主笔决定标题和篇幅.

规则随任务读取. 新写文章会读取选材和结构规则, 局部改写只读取相关语言规则. 写一篇普通文章时, 不生成评分表, 过程标签或多轮评审记录.

需要阅读源码时, cleanread 将数据库和查询记录保存在草稿所在的外部目录, 不写入源码项目. 它先找到相关定义和调用关系, 再读取必要片段. 主笔根据这些事实说明对象做了什么, 在什么条件下发生, 执行顺序怎样. 正文不照抄源码结构.

## 仓库内容

- `SKILL.md` 定义写作任务, 材料范围和执行流程.
- `modules/` 保存内容, 结构, 语言和格式规则.
- `scripts/rule_index.py` 按章节加载当前任务需要的规则.
- `scripts/check_output_format.py` 检查中文标点, 空格和字符格式.
- `tools/cleanread/` 为源码写作任务建立索引并读取必要片段.

规则可以单独查询:

```powershell
uv run --python 3.12 scripts/rule_index.py sections --module language
uv run --python 3.12 scripts/rule_index.py section "modules/language.md#句式"
```

交付文章前可以检查格式:

```powershell
uv run --python 3.12 scripts/check_output_format.py README.md --punctuation ascii
```

