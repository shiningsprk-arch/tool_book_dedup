# -*- coding: utf-8 -*-
"""查重合并的判定引擎 —— 纯函数，零宿主依赖。

这里的分层是刻意的：所有"怎么算"都在本包内，`driver.py` 只负责"怎么取数据、怎么落盘"。
好处是整个引擎可以在没有 MyBooks、没有 calibre 的机器上跑单测与离线 smoke。

    normalize   归一化（ISBN / 标题 / 作者 / 媒体家族）
    similarity  字符 n-gram Dice 相似度
    cluster     候选择配对 + 并查集分组
    metadata    元数据完整度评分
    diff        只展开有差异的字段
    keeper      推荐保留哪一本 + 理由枚举
    report      报告形状 / 汇总 / 筛选

设计对照 BookOrbit 的 `book-duplicates` 模块（AGPL-3.0），**全部 clean-room 重写**：
借的是判定思路与结构（理由枚举、字段折叠、两档证据、误报记忆），不复制其代码、
不照抄其权重表与停用词表。
"""

from . import cluster, diff, keeper, metadata, normalize, report, similarity

__all__ = ['cluster', 'diff', 'keeper', 'metadata', 'normalize', 'report', 'similarity']