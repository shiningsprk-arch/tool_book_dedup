# -*- coding: utf-8 -*-
"""忽略名单：把"这几本不是重复"记下来，下次扫描不再报出来。

**为什么记"配对"而不是记"书的 id"**（用户提的是 id 白名单，这里实现得更窄一点，
理由两条，都是防误伤）：

1. 记整本会**连真重复一起藏掉**。书 A 与 B 被忽略之后，A 若在别处还有一份真重复 C，
   整本忽略会让 A-C 也消失——用户说的是"这两本不是重复"，不是"这本永远不查"。
2. 书 id **会被复用**。calibre 的新书拿到 `max(id)+1`，删掉的 id 之后可能被一本
   完全无关的新书占上（本工具的合并就会删记录）。只存 id 的话，这份白名单会静默
   把一本新书当成"已忽略"。所以每条配对**同时存两侧的指纹**（书名+作者+体积），
   扫描时指纹对不上就判定这条已失效、剔掉并在报告里交代。

忽略的作用点只有一处：**配对生成之后、分组之前**剔除被忽略的配对。分组是"成对关系的
连通分量"，去掉几条边只会把组拆小或拆没，不会凭空把两组并起来——所以界面上"忽略之后
这一行立刻消失"与"重新查重之后不出现"是同一件事（对整组点忽略 = 组内两两都记上）。
"""
# 单条配对的指纹里带哪些字段：书名 + 第一作者 + 体积。刻意不用 id 之外的"更稳"字段——
# 这三项是用户可见的、也是"同一本书的另一份"最容易变的（体积差一点不算换书，
# 所以体积只取约数，见 `signature`）。
_SIG_ROUND = 64 * 1024      # 体积按 64KB 取整：重新导入同一本书体积会有细微差别


def pair_key(left_id, right_id):
    """一对配对的规范键（小的在前，与 `cluster._make_pair` 同规矩）。"""
    left, right = int(left_id), int(right_id)
    return (left, right) if left <= right else (right, left)


def pairs_in(ids):
    """一组 id 内**两两**配对（组内每两条记录都算一对"不是重复"）。

    对整组点"忽略"就是这个语义：这一组里的任意两本都不是重复，
    所以把组内所有配对都记上，重新查重时它们不会再被连成一组。
    """
    unique = sorted({int(i) for i in (ids or [])})
    return [pair_key(unique[i], unique[j])
            for i in range(len(unique)) for j in range(i + 1, len(unique))]


def signature(record):
    """一条记录的指纹：书名 | 主作者 | 体积（取整）。用来察觉 id 换了主人。"""
    if not record:
        return ''
    authors = record.get('authors') or []
    author = str(authors[0]) if authors else ''
    size = int(record.get('size') or 0)
    return '%s|%s|%d' % (record.get('title') or '', author, size // _SIG_ROUND)


def entry_is_live(entry, records_by_id):
    """这条忽略记录现在还算数吗（两侧都还在、指纹都没变）。

    :param records_by_id: {book_id: 本次扫描读到的记录}
    :return: True / False（False 的三种理由：书没了、id 换了主人、记录里没这本书）
    """
    for side in ('a', 'b'):
        book_id = entry.get(side)
        record = records_by_id.get(book_id)
        if record is None:
            return False        # 书已不在书库 → 这条没有意义了
        expected = (entry.get('sigs') or {}).get(str(book_id))
        if expected is None:
            continue            # 旧版本写的条目没带指纹：按"还在"处理，别误剔
        if expected != signature(record):
            return False        # 同一个 id 换了一本书 → 不许拿它当白名单
    return True


def filter_pairs(pairs, ignored_keys):
    """按忽略名单剔掉配对。

    :return: `(kept_pairs, dropped_count)`
    """
    if not ignored_keys:
        return list(pairs), 0
    kept, dropped = [], 0
    for pair in pairs:
        if pair_key(pair['a'], pair['b']) in ignored_keys:
            dropped += 1
            continue
        kept.append(pair)
    return kept, dropped


def ignored_keys_from(entries):
    """忽略条目列表 → 配对键集合（只取结构完整的）。"""
    keys = set()
    for entry in entries or []:
        if entry.get('a') is None or entry.get('b') is None:
            continue
        keys.add(pair_key(entry['a'], entry['b']))
    return keys


def counts_for_group(member_ids, ignored_keys):
    """这一组里有多少对已被忽略、总共多少对。界面据此决定"藏掉这一行"还是"提示一句"。"""
    all_pairs = pairs_in(member_ids)
    if not all_pairs:
        return {'pairs': 0, 'ignored': 0}
    ignored = sum(1 for key in all_pairs if key in ignored_keys)
    return {'pairs': len(all_pairs), 'ignored': ignored}