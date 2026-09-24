# 查重合并（book_dedup）

MyBooks 工具箱外置工具：找出书库里的重复书籍，逐组对照后把重复项的格式并入保留项，
并（可选）删除重复记录。

- 工具 ID：`book_dedup`
- 形态：外置工具包（`manifest.json` + `backend/` + `frontend/` + `icon.png`），用 `mytool` 打包
- CoreAPI：`1.3.0`
- 授权：BSD-2-Clause（判定思路参考 BookOrbit，**未使用其代码** —— 见 `backend/NOTICE`）

## 安装

1. 在 MyBooks 里开启开发者模式（`ENABLE_TOOLBOX_DEV_MODE`）
2. 工具箱 → 上传工具包 → 选择 `dist/book_dedup-<版本>.zip`
3. **安装/更新需要重启 MyBooks 才生效**（禁用/启用即时生效）
4. 装完如果界面看起来还是旧版，硬刷一次工具页——宿主的工具前端资源 handler 不设缓存头，
   但本工具的 `index.html` 里资源带 `?v=<revision>`，改版本时会自动换掉

## 怎么用

1. **开始查重**：默认扫描全库。相似度阈值默认 85（越高越保守），结果里会显示实际使用的值。
2. **看结果**：列表每行**直接列出成员书名**（每行最多 2 个，其余点开看全部）与作者，
   并显示证据强度（确定 / 很可能 / 存疑）、可回收空间。
3. **展开一组**：点该行——对照台**就在这一行之内展开**（不是弹窗，不会盖住列表、
   也不会重置滚动位置）。对照表只列出成员之间**有差异**的字段，相同的折叠成一句摘要；
   两份完全相同时会明说「除差异项外其余都相同」。
4. **选保留哪本**：工具按规则给推荐（元数据最全 → 格式最多 → 体积最大 → 最早入库）并
   写明理由；点对照卡上的「保留这本」可覆盖它——改选后标题会如实变成「保留这本：X · 你手动选择」，
   因为原来的推荐理由已经不对了。
5. **合并**：点「先看合并预览」（同一行内就地展开），确认后执行。预览会列出
   **会复制过去的格式**与**会被丢弃的同名格式**——同名格式不会被复制（见下）。
   ESC 或再点一次行头即可收起。

## 判定规则

两档证据，从强到弱：

| 等级 | 判据 | 说明 |
|---|---|---|
| 确定 | ISBN 相同 + 媒体家族相同 | ISBN-10 会换算成 ISBN-13 再比，所以新老书号能对上；校验位不对的一律丢弃 |
| 很可能 | 标题相似度 ≥ 阈值 **且** 作者有交集 | 作者不同的一对根本不进入标题比较 |
| 存疑 | 仅标题相似 | 多半是误报（同系列/同作者），默认折叠 |

中文文本的处理是本工具自己定的：

- 标题先做归一化：NFKC 折全角、去标点与空白、小写
- 相似度取「完整标题的字符 n-gram Dice 与包含度的较小值」和「剥离尾部注记后的主书名」
  两个视角的较大者——`《三体》` 与 `《三体（全集）》` 判为同一本，
  而 `《三体》` 与 `《三体2》`（同系列不同卷）不会被判为重复
- 中文用字符 bigram，西文用 trigram；两者都不用分词器（分词结果依赖切分一致性，
  对"同一本书的两种写法"不鲁棒）

## 合并做了什么，以及**没做什么**

**做了**：

1. 把重复项的格式文件复制到保留项（只复制保留项缺的格式）
2. 可选：删除重复记录

**没做（重要）**：

- **同名格式不会被复制。** 两本都有 EPUB 时，合并后留下的是**保留项的那一份**，
  源书那份会被丢弃。预览里会明确列出"会被丢弃：EPUB"，但这一步是不可逆的。
- **源记录的用户数据不会被迁移。** 收藏 / 在读 / 阅读进度 / 时长 / 评分 / 书评 / 标注 /
  书单归属都挂在被删掉的 book_id 上，删除不会把这些搬到保留项上。
  根因是工具箱的删除只清 `Item`（上游 issue
  [PoxenStudio/mybooks#82](https://github.com/PoxenStudio/mybooks/issues/82) 未修），
  而 `CoreAPI.db` 只暴露 4 个方法，工具读不到也写不了那些表。
  **所以删除前请自己确认要保留哪一本。**
- **不勾"同时删除重复记录"时**，只做格式合并，两条记录都留着（之后可自行处理）。

## 开发

```bash
# 单测（纯引擎 + 假宿主 + 前端契约，共 87 项；不需要 MyBooks / calibre）
python tests/test_dedup_core.py
python tests/test_fake_host.py
python tests/test_frontend_contract.py

# 离线 smoke：拿真 calibre 书库跑完整流程
python scripts/smoke_offline.py --all      # 干净库 0 误报 + 注入库全命中 + 25k 本性能

# 本地浏览器预览（真报告数据 + stub 桥）
python scripts/make_preview.py
python -m http.server 8765 -d dev/preview  # http://127.0.0.1:8765/index.html

# 打包（清理字节码 + 打包 + 形状校验）
python scripts/build.py [--check] [--mytool]

# 重新生成图标 / 三语文案
python scripts/make_icon.py
python scripts/make_locales.py
```

### 一条别再犯的界面约束

**详情（对照台与合并预览）必须是行内抽屉，不许回到居中浮层。** 0.1.1 之前用的是
`position: fixed; inset: 0; display: flex; align-items: center` 的浮层，真机反馈
「报告总是在最中间显示、盖住列表」。现在渲染在被点那一行之内（`.bd-drawer`），
`tests/test_frontend_contract.py::test_overlays_are_gone` 会守住这一点。
另外**改保留项只许重画抽屉**（`refreshDrawer()`），调 `loadGroups()` 全量重画会把
滚动位置重置掉——同文件 `test_keeper_pick_does_not_rerender_whole_list` 守着。

### 结构

```
backend/
  tool.py            BookDedupTool + 8 个 Handler（@js @is_admin）
  driver.py          取数（分批读全库）、扫描编排、报告落盘、合并计划
  merge.py           合并的预览/执行与"已处理"记账（**唯一的写路径**）
  dedup/             ★ 纯函数引擎，零宿主依赖，全部可离线单测
    normalize.py     ISBN 校验与 10↔13 换算、标题/作者归一化、媒体家族
    similarity.py    字符 n-gram Dice、变体判定（含尾部注记剥离）
    cluster.py       候选择配对（作者桶短路）+ 并查集分组
    metadata.py      元数据完整度评分
    diff.py          只展开有差异的字段
    keeper.py        推荐保留哪一本 + 理由枚举
    report.py        报告形状 / 汇总 / 筛选
frontend/            自包含静态页（无构建步骤）
```

### 为什么性能上不会爆

宿主仓库自带 `tests/cases/big-metadata.db` 是 **24,835 本**，两两比较是 3 亿次。
所以候选生成走"作者桶短路"：先按归一化作者建倒排，**只在共享作者的桶内比标题**，
作者不同的一对根本不比较。单个作者桶超过上限时跳过并记账（在报告的 `stats` 里如实交代），
宁可不比也不让一次扫描把服务拖住。

实测（`scripts/smoke_offline.py --perf`，25k 本合成库、作者池 200 人）：**约 0.4 秒**。

## 还没在真机验证的部分

- 8 条路由在宿主里的挂载与鉴权（本机无 MyBooks，只做了宿主符号静态核对 + 假宿主 harness）
- 真书库上的整库扫描耗时（离线用的是 calibre `metadata.db` 直读，绕过了宿主一段）
- `window.open('/book/<id>')` 在宿主 iframe 里的实际行为（桥没有导航通道，
  因此走新标签页；这一点已按宿主源码确认，但未在真宿主里点过）

## 出处

判定思路参考 [BookOrbit](https://github.com/bookorbit/bookorbit)（AGPL-3.0）的重复检测模块。
**本工具是独立实现，不含其任何代码、权重表或停用词表** —— 逐条说明见 `backend/NOTICE`，
因此不引入 AGPL 义务。