# -*- coding: utf-8 -*-
"""打包查重合并工具为可直接上传的 zip。

宿主只读归档根目录，因此 zip 里必须是 `manifest.json` + `backend/` + `frontend/` + `icon.png`
（外加 LICENSE 与 backend/NOTICE），不能套一层文件夹。

为什么自己打包而不是调 `mytool build`：离线可复现、无需 npm，并且顺带做一轮产物形状校验
（混进字节码、缺文件、manifest 指向不存在的模块，都在构建期就报错，而不是装完才发现）。
需要脚手架产物时再跑 `--mytool`。

用法：
    python scripts/build.py                 # 清理 → 打包 → 校验
    python scripts/build.py --check         # 只校验现有产物
    python scripts/build.py --out dist      # 指定输出目录
    python scripts/build.py --mytool        # 额外用官方脚手架校验（需 node/npx）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 归档根的允许条目（多一层或少一层都会被宿主判为非法包）
ROOT_ENTRIES = {'manifest.json', 'icon.png', 'LICENSE', 'backend', 'frontend'}
REQUIRED = (
    'manifest.json',
    'icon.png',
    'LICENSE',
    # 出处与未使用声明：放 backend/ 里以保持顶层包形状不变
    'backend/NOTICE',
    'backend/__init__.py',
    'backend/tool.py',
    'backend/driver.py',
    'backend/merge.py',
    # 判定引擎：漏打任何一个都会让工具在运行时 ImportError
    'backend/dedup/__init__.py',
    'backend/dedup/normalize.py',
    'backend/dedup/similarity.py',
    'backend/dedup/cluster.py',
    'backend/dedup/metadata.py',
    'backend/dedup/diff.py',
    'backend/dedup/keeper.py',
    'backend/dedup/report.py',
    'frontend/index.html',
    'frontend/app.js',
    'frontend/lib/theme.css',
    'frontend/lib/book_dedup.css',
    'frontend/locales/zh.json',
    'frontend/locales/en.json',
    'frontend/locales/zh-TW.json',
)
FORBIDDEN_PARTS = ('__pycache__', '.pyc', '.pyo', '.DS_Store', '.git', 'dev/', 'tests/')
# 开发期才用的东西不进包：本地预览与测试
EXCLUDE_DIRS = {'dev', 'tests', 'scripts', '.pytest_cache', '__pycache__'}
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def clean_bytecode():
    removed = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        if os.sep + '.git' in dirpath:
            continue
        for name in list(dirnames):
            if name == '__pycache__':
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.endswith(('.pyc', '.pyo')):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def load_manifest():
    path = os.path.join(REPO_ROOT, 'manifest.json')
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def validate_source():
    """构建前检查源目录：manifest 合法、必需文件在、入口模块可解析。"""
    errors = []
    manifest = load_manifest()
    for key in ('tool_id', 'name', 'revision', 'core_api_version',
                'entry_backend', 'entry_frontend', 'api_routes'):
        if not manifest.get(key):
            errors.append('manifest.json 缺字段：%s' % key)

    for relative in REQUIRED:
        if not os.path.exists(os.path.join(REPO_ROOT, relative)):
            errors.append('缺文件：%s' % relative)

    # entry_backend 形如 "tool.BookDedupTool"：模块与类都要真的存在
    entry = manifest.get('entry_backend') or ''
    if '.' in entry:
        module_name, class_name = entry.rsplit('.', 1)
        module_path = os.path.join(REPO_ROOT, 'backend', module_name + '.py')
        if not os.path.exists(module_path):
            errors.append('entry_backend 指向不存在的模块：%s' % module_path)
        else:
            with open(module_path, 'r', encoding='utf-8') as handle:
                source = handle.read()
            if not re.search(r'^class\s+%s\b' % re.escape(class_name), source, re.M):
                errors.append('entry_backend 的类不存在：%s' % class_name)

    # api_routes 的 handler 同样要存在
    tool_py = os.path.join(REPO_ROOT, 'backend', 'tool.py')
    if os.path.exists(tool_py):
        with open(tool_py, 'r', encoding='utf-8') as handle:
            tool_source = handle.read()
        for route in manifest.get('api_routes') or []:
            handler = (route.get('handler') or '').rsplit('.', 1)[-1]
            if handler and not re.search(
                    r'^class\s+%s\b' % re.escape(handler), tool_source, re.M):
                errors.append('api_routes 的 handler 不存在：%s' % handler)

    return manifest, errors


def iter_payload():
    """产出 (归档内路径, 磁盘路径)。"""
    for name in sorted(os.listdir(REPO_ROOT)):
        if name in EXCLUDE_DIRS or name in ('.git', '.gitignore', '.gitattributes'):
            continue
        full = os.path.join(REPO_ROOT, name)
        if os.path.isfile(full):
            if name in ROOT_ENTRIES or name == 'LICENSE':
                yield name, full
            continue
        # 目录：只收 backend / frontend
        if name not in ('backend', 'frontend'):
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in EXCLUDE_DIRS and d != '__pycache__')
            for filename in sorted(filenames):
                if filename.endswith(('.pyc', '.pyo', '.pyo')):
                    continue
                disk = os.path.join(dirpath, filename)
                relative = os.path.relpath(disk, REPO_ROOT).replace(os.sep, '/')
                yield relative, disk


def build(out_dir, keep=False):
    manifest, errors = validate_source()
    if errors:
        for error in errors:
            print('  ✗ %s' % error)
        raise SystemExit('源目录校验失败，未打包')

    if os.path.exists(out_dir) and not keep:
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    stamp = (1980, 1, 1, 0, 0, 0)   # 固定时间戳：让同一份源码的 zip 字节可复现
    archive = os.path.join(
        out_dir, '%s-%s.zip' % (manifest['tool_id'], manifest['revision']))

    entries = []
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for relative, disk in iter_payload():
            info = zipfile.ZipInfo(relative, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            # Windows 与 Linux 打出的字节要与 CI 一致：不固定 create_system 会差一个字节
            info.create_system = 0
            with open(disk, 'rb') as handle:
                zf.writestr(info, handle.read())
            entries.append(relative)

    problems = validate_archive(archive, manifest)
    sha = hashlib.sha256(open(archive, 'rb').read()).hexdigest()
    return archive, entries, problems, sha


def validate_archive(archive, manifest):
    """产物形状校验：条目合规、必需文件在、没有字节码、图标是真 PNG。"""
    problems = []
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        for name in names:
            if any(part in name for part in FORBIDDEN_PARTS):
                problems.append('不该进包：%s' % name)
            top = name.split('/')[0]
            if '/' in name and top not in ('backend', 'frontend'):
                problems.append('多余的顶层目录：%s' % name)
            if '/' not in name and name not in ROOT_ENTRIES and name != 'LICENSE':
                problems.append('多余的顶层文件：%s' % name)
        for relative in REQUIRED:
            if relative not in names:
                problems.append('产物缺文件：%s' % relative)
        if 'icon.png' in names:
            head = zf.read('icon.png')[:8]
            if head != PNG_MAGIC:
                problems.append('icon.png 不是 PNG（宿主动态工具只认 PNG）')
        if 'manifest.json' in names:
            in_zip = json.loads(zf.read('manifest.json').decode('utf-8'))
            for key in ('tool_id', 'revision', 'entry_backend'):
                if in_zip.get(key) != manifest.get(key):
                    problems.append('manifest %s 与源不一致' % key)
    return problems


def run_mytool(archive):
    """可选：用官方脚手架再校验一次（需要 node/npx）。"""
    try:
        result = subprocess.run(
            ['npx', '--yes', 'mytool', 'validate', archive],
            capture_output=True, text=True, shell=(os.name == 'nt'))
    except FileNotFoundError:
        return False, 'npx 不可用，跳过官方校验'
    output = (result.stdout or '') + (result.stderr or '')
    return result.returncode == 0, output.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=os.path.join(REPO_ROOT, 'dist'))
    parser.add_argument('--check', action='store_true', help='只校验现有产物')
    parser.add_argument('--mytool', action='store_true', help='额外跑官方 mytool validate')
    parser.add_argument('--keep', action='store_true', help='保留 dist 里已有的包')
    args = parser.parse_args()

    manifest = load_manifest()
    archive = os.path.join(
        args.out, '%s-%s.zip' % (manifest['tool_id'], manifest['revision']))

    if args.check:
        if not os.path.exists(archive):
            raise SystemExit('产物不存在：%s' % archive)
        problems = validate_archive(archive, manifest)
    else:
        removed = clean_bytecode()
        print('清理字节码：%d 处' % removed)
        archive, entries, problems, sha = build(args.out, keep=args.keep)
        print('打包：%s' % archive)
        print('条目：%d' % len(entries))

    if problems:
        for problem in problems:
            print('  ✗ %s' % problem)
        raise SystemExit('产物校验失败')

    with open(archive, 'rb') as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    print('sha256：%s' % digest)
    print('大小：%.1f KB' % (os.path.getsize(archive) / 1024.0))

    if args.mytool:
        ok, output = run_mytool(archive)
        print('官方 mytool validate：%s' % ('通过' if ok else '未通过/跳过'))
        if output:
            print(output)

    print('校验通过 ✓')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())