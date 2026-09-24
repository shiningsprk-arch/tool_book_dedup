# 打包产物与本地预览

- `dist/`：**入库分发**。每个版本的 zip 都在这里，从仓库直接下载到的就是这个，
  不必自己搭环境重打。
- `dev/`：本地浏览器预览用，不入库（`scripts/make_preview.py` 生成）。

## 产物说明

| 文件 | 说明 |
|---|---|
| `book_dedup-<版本>.zip` | 工具包本体（`manifest.json` + `backend/` + `frontend/` + `icon.png`，扁平根目录），上传到 MyBooks 工具箱即可安装 |
| `SHA256SUMS.txt` | 当前版本的校验和 |

## 自己核一遍

```bash
# Windows
certutil -hashfile dist/book_dedup-0.1.4.zip SHA256
# Linux / macOS
sha256sum -c dist/SHA256SUMS.txt      # 在仓库根目录执行
```

## 为什么从源码重打能得到同一份字节

`scripts/build.py` 固定了时间戳、条目顺序、权限位与 zip 头的 `create_system`，
并要求文本文件是 LF 行尾，所以**同一份源码在任何平台打出的 zip 是逐字节一致的**：

```bash
git clone https://github.com/shiningsprk-arch/tool_book_dedup.git
cd tool_book_dedup
python scripts/build.py
# sha256 应与 dist/SHA256SUMS.txt 相同
```