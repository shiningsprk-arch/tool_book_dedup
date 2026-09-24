# -*- coding: utf-8 -*-
"""前端契约：守住"代码里用到的每个 i18n 键都有三语翻译"。

漏键不会报错，只会让界面在运行时漏出原始 key（`scope.thresholdHint` 这种），
而这类问题只有在浏览器里点到那个分支才会暴露。所以在这里静态对账。

同时守住几条容易回归的接线：资源相对路径、`?v=` 版本串与 manifest 一致、
以及 `.bd-overlay` 必须被 `[hidden]` 压住（quality_check 踩过：空弹层一直挂在页面上挡点击）。

运行：python tests/test_frontend_contract.py
"""
import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
FRONTEND = os.path.join(ROOT, 'frontend')
LOCALES = os.path.join(FRONTEND, 'locales')


def read(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def strip_comments(text):
    """去掉注释，只留"活的代码"——注释里提到某个已删的类名不该触发断言。"""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)      # HTML
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)      # 块注释（JS/CSS）
    text = re.sub(r'(?m)^\s*//.*$', '', text)               # 行首行注释（JS）
    return text


def used_keys():
    """前端实际引用的 i18n 键：t('..')、data-i18n、data-i18n-attr，以及几张映射表。"""
    js = read(os.path.join(FRONTEND, 'app.js'))
    html = read(os.path.join(FRONTEND, 'index.html'))
    keys = set(re.findall(r"\bt\(\s*'([a-zA-Z][a-zA-Z0-9_.]*)'", js))
    keys |= set(re.findall(r'data-i18n="([^"]+)"', html))
    keys |= set(re.findall(r'data-i18n-attr="[^:]+:([^"]+)"', html))
    for table in re.finditer(
            r"var (REASON_KEYS|CONFIDENCE_KEYS|KEEPER_REASON_KEYS|PHASE_KEYS) = \{(.*?)\};",
            js, re.S):
        keys |= set(re.findall(r":\s*'([a-zA-Z][a-zA-Z0-9_.]*)'", table.group(2)))
    # 汇总条的 `['summary.groups', '重复分组', value]` 形式：数组首元素是键
    keys |= set(re.findall(r"\[\s*'([a-z]+\.[a-zA-Z][a-zA-Z0-9_.]*)',", js))
    # `t('key', '兜底文案')` 的兜底不算键；'div' 是表里混进来的 CSS 类名残留
    keys.discard('div')
    return keys


class TestLocales(unittest.TestCase):
    def setUp(self):
        self.tables = {}
        for code in ('zh', 'en', 'zh-TW'):
            path = os.path.join(LOCALES, code + '.json')
            self.assertTrue(os.path.exists(path), '缺少 %s.json' % code)
            self.tables[code] = json.loads(read(path))
        self.used = used_keys()

    def test_every_used_key_is_translated(self):
        missing = {}
        for code, table in self.tables.items():
            gap = sorted(self.used - set(table))
            if gap:
                missing[code] = gap
        self.assertEqual(missing, {}, '这些键在代码里用到但没有翻译：%s' % missing)

    def test_locales_have_identical_key_sets(self):
        base = set(self.tables['zh'])
        for code, table in self.tables.items():
            self.assertEqual(set(table), base,
                             '%s 的键集与 zh 不一致' % code)

    def test_no_unused_keys(self):
        """翻译里多余的键通常是改名后忘了删——留着会让人以为界面上有那段文案。"""
        unused = {}
        for code, table in self.tables.items():
            extra = sorted(set(table) - self.used)
            if extra:
                unused[code] = extra
        self.assertEqual(unused, {}, '这些键有翻译但代码里没用到：%s' % unused)

    def test_placeholders_match_across_locales(self):
        """`{n}` / `{title}` 这类占位符三语必须一致，否则某语言下会漏出字面量。"""
        pattern = re.compile(r'\{([a-zA-Z_]+)\}')
        problems = {}
        for key, text in self.tables['zh'].items():
            expected = set(pattern.findall(str(text)))
            for code in ('en', 'zh-TW'):
                found = set(pattern.findall(str(self.tables[code][key])))
                if found != expected:
                    problems.setdefault(key, {})[code] = sorted(found ^ expected)
        self.assertEqual(problems, {}, '占位符不一致：%s' % problems)


class TestFrontendWiring(unittest.TestCase):
    def setUp(self):
        self.js = read(os.path.join(FRONTEND, 'app.js'))
        self.html = read(os.path.join(FRONTEND, 'index.html'))
        self.css = read(os.path.join(FRONTEND, 'lib', 'book_dedup.css'))
        self.manifest = json.loads(read(os.path.join(ROOT, 'manifest.json')))

    def test_bridge_is_the_real_global(self):
        """宿主的桥挂在 window.MyBooksToolBridge 上（不是 toolboxBridge）。"""
        self.assertIn('window.MyBooksToolBridge', self.js)
        self.assertNotIn('window.toolboxBridge', self.js)

    def test_theme_is_string_not_object(self):
        """bridge.theme / bridge.locale 是字符串，变化走 onThemeChange/onLocaleChange。"""
        self.assertNotIn('bridge.theme.watch', self.js)
        self.assertNotIn('bridge.locale.get', self.js)
        self.assertIn('onThemeChange', self.js)
        self.assertIn('onLocaleChange', self.js)

    def test_version_query_matches_manifest(self):
        revision = self.manifest['revision']
        for match in re.finditer(r'(?:src|href)="([^"]+)"', self.html):
            url = match.group(1)
            if url.startswith('/static/'):
                continue          # 桥由宿主提供，不带版本串
            self.assertIn('?v=' + revision, url,
                          '资源 %s 的 ?v= 与 manifest revision 不一致' % url)

    def test_assets_exist(self):
        for match in re.finditer(r'(?:src|href)="([^"]+)"', self.html):
            url = match.group(1).split('?')[0]
            if url.startswith('/static/') or url.startswith('http'):
                continue
            path = os.path.join(FRONTEND, url)
            self.assertTrue(os.path.exists(path), '资源不存在：%s' % url)

    def test_hidden_beats_overlay_flex(self):
        """`.bd-overlay{display:flex}` 会盖过 `[hidden]` 的默认 display:none —— 必须显式压住。"""
        self.assertIn('display: flex', self.css)
        self.assertRegex(self.css, r'\[hidden\]\s*\{\s*display:\s*none\s*!important')

    def test_theme_variables_are_used_not_hardcoded(self):
        """颜色一律走 --mb-color-*，否则深色模式下会有看不清的地方。"""
        offenders = re.findall(r':\s*#[0-9a-fA-F]{3,6}', self.css)
        # 只允许极少数不可避免的字面量（如遮罩的半透明黑）
        self.assertLessEqual(len(offenders), 1,
                             '样式里出现了硬编码颜色：%s' % offenders)

    def test_overlays_are_gone(self):
        """**回归**：详情不许回到居中浮层。

        真机反馈"报告总是在最中间显示"——根因是 `.bd-overlay` 的 `position:fixed;inset:0;
        display:flex;align-items:center` 把内容钉在屏幕正中、盖住列表。详情现在渲染在
        被点那一行之内（`.bd-drawer`），所以浮层 DOM、样式、引用都不该再出现。

        查的是**剥掉注释后的代码**：文件名里保留一段"为什么删掉浮层"的说明是有价值的，
        不该因为提到这几个词就把测试判红。
        """
        targets = (('index.html', strip_comments(self.html)),
                   ('app.js', strip_comments(self.js)),
                   ('book_dedup.css', strip_comments(self.css)))
        for label, text in targets:
            for needle in ('bd-overlay', 'bd-sheet', 'bench-overlay', 'plan-overlay'):
                self.assertNotIn(needle, text, '%s 里还有活的 %s 引用' % (label, needle))

    def test_drawer_renders_inside_its_row(self):
        """抽屉必须由行模板生成，而不是追加到列表末尾/单独的浮层节点。"""
        self.assertIn("'<section class=\"bd-drawer\">'", self.js)
        # 行模板里带 data-group，重画时靠它定位到"哪一行"
        self.assertIn('data-group=\"', self.js)
        self.assertIn('bd-drawer', self.css)

    def test_row_title_lists_member_titles(self):
        """列表行要直接列书名——不能只有"x 本可能是同一本书"。"""
        self.assertIn('group.titles', self.js)
        self.assertIn('preview_truncated', self.js)
        self.assertIn('list.membersMore', self.js)
        # 旧报告没有 titles 时要有回落，别因为升级就显示空白
        self.assertIn("t('list.groupTitle'", self.js)

    def test_keeper_pick_does_not_rerender_whole_list(self):
        """改保留项只重画抽屉。

        原来 `pickKeeper()` 里调 `loadGroups()` 全量重画，会把滚动位置重置掉——
        正在看的那一行会跳走。
        """
        match = re.search(r'function pickKeeper\(bookId\) \{(.*?)\n  \}', self.js, re.S)
        self.assertIsNotNone(match, '找不到 pickKeeper 的定义')
        body = match.group(1)
        self.assertIn('refreshDrawer()', body)
        self.assertNotIn('loadGroups()', body)

    def test_no_absolute_api_paths(self):
        """工具接口必须走桥（相对路径）；唯一例外是本地预览的兜底分支。

        写死绝对路径会在宿主换前缀时失效，所以只允许一处、且必须出现在
        `api()` 的 bridge 兜底里（本地预览用，宿主里走不到）。
        """
        occurrences = self.js.count("'/api/toolbox")
        self.assertEqual(occurrences, 1,
                         '除了预览兜底，不该有别的绝对接口路径（实际 %d 处）' % occurrences)
        fallback = self.js.split('function api(path, options)')[1].split('function notify')[0]
        self.assertIn("'/api/toolbox", fallback)
        self.assertIn('bridge.fetch', fallback)
        self.assertIn("'/api/toolbox/tool/' + TOOL_ID + '/' + path", fallback)


if __name__ == '__main__':
    unittest.main(verbosity=2)