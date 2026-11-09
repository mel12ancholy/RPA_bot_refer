# RPA_bot_refer

影刀 `.bot` 指令文件批量重打包工具。

它给每个 `.bot` 生成全新的 `uuid` 与指令集编码，并重新生成 `package.sigstore` 签名，
使**原文件与处理后的文件可以被同一组织内不同账号同时导入**（解决「导入失败，存在版本冲突」）。
适用于影刀 5.25.66 企业版 + .NET 10 SDK 环境。

> 本仓库**只保存源码**，不包含任何 `.exe`。请在公司电脑上按下方步骤自行打包。

---

## 一、环境要求

在**打包并运行 exe 的那台电脑**上需要具备：

| 依赖 | 说明 |
| --- | --- |
| Windows + Python 3.10+ | 用于打包（源码用了 `str \| None` 类型注解） |
| PyInstaller | `pip install pyinstaller` |
| 影刀客户端 | 提供 `ShadowBot.Runtime.dll`；共享目录部署时可直接把 DLL 拷到 exe 同目录，见第五节 |
| .NET SDK 8 / 9 / 10 | 编译重签用的临时 C# 程序；脚本会自动探测 `dotnet.exe` |

exe 本身**不打包** .NET SDK 和影刀 Runtime，这两样必须装在运行的机器上。

---

## 二、打包前必改的配置

用文本编辑器打开 `bot_repack.py`，修改文件顶部的配置区：

```python
RUNTIME_DLL = r"E:\shadowbot\ShadowBot Private\shadowbot-5.25.66\ShadowBot.Runtime.dll"
```

改成公司电脑上影刀的实际安装路径（也可设为 `None` 让脚本自动探测 `C:\Program Files\ShadowBot\`）。

其它常用配置：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `NEW_UUID` | `"auto"` | 每个文件生成全新 uuid；`None` = 不改 |
| `NEW_CODE` | `"auto"` | 自动识别并替换 `activity_xxx` 指令集编码；`None` = 不改 |
| `NEW_VERSION` | `"auto"` | 原版本末位 +1（如 `26.11.0` → `26.11.1`）；也可填 `"26.11.2"` |
| `OUTPUT_DIR` | `"repacked"` | 输出子目录名 |
| `SCAN_DIR` | `None` | 默认 exe 所在文件夹 |
| `DO_SIGN` | `True` | 是否重签；改成 `False` 只改包不签名 |
| `DOTNET_EXE` | `None` | 自动探测失败时手动指定 `dotnet.exe` |
| `TFM` | `None` | 按已装 SDK 自动选；可强制 `net8.0` / `net10.0` |

注意 `RUNTIME_DLL` 用了 `r"..."` 原始字符串，路径里的反斜杠不会被当成转义符。

> 如果 exe 要放到**公司共享目录给同事用**，这个本机路径在别人电脑上不存在，不必改：脚本会优先使用 exe 同目录的 `ShadowBot.Runtime.dll`，详见「五、共享目录部署」。

---

## 三、打包成 exe

在仓库目录打开命令行：

```bash
pip install pyinstaller
pyinstaller --onefile --console --name bot_repack --clean bot_repack.py
```

产物在 `dist\bot_repack.exe`。

- Python 3.13 请使用 **PyInstaller 6.11+**（更旧版本不支持 3.13）。
- 若公司内网 pip 源里没有这个包，可指定官方源：`pip install pyinstaller -i https://pypi.org/simple`。

- `--onefile` 生成单个 exe，方便拷贝。
- `--console` 保留控制台窗口，能看到每个文件的处理日志和重签结果；**不要改成 `--windowed`**，否则出错时看不到信息。
- 打包生成的 `build\`、`dist\`、`bot_repack.spec` 已被 `.gitignore` 排除，不要提交。

---

## 四、使用方式

### 双击运行（推荐）

把 `dist\bot_repack.exe` 复制到**存放 `.bot` 文件的那个文件夹**，双击即可：

1. 扫描 exe 所在文件夹下的全部 `*.bot`（不递归，不会重复处理 `repacked\` 里的结果）。
2. 自动创建 `repacked\` 文件夹（已存在则复用）。
3. 逐个处理，结果按原文件名放入 `repacked\`。
4. 同名文件已存在时自动改为 `xxx_2.bot`、`xxx_3.bot`……再运行一次就会多一份**全新 uuid** 的副本，方便分发给更多账号。
5. 某个文件失败不影响其它文件，结束后打印成功/失败汇总。
6. 原始 `package.sigstore` 备份到 exe 旁的 `_sigstore_backups\`（带时间戳，不会被覆盖）。

```
D:\影刀指令库\
├── bot_repack.exe
├── 指令A.bot
├── 指令B.bot
└── repacked\            ← 自动创建
    ├── 指令A.bot        ← 全新 uuid + 新编码 + 已重签
    └── 指令B.bot
```

### 命令行方式

```bash
# 扫描当前 exe / 脚本所在文件夹（默认行为）
python bot_repack.py

# 指定扫描目录与输出目录
python bot_repack.py --scan-dir D:\影刀指令库 --output-dir D:\output

# 只处理单个文件
python bot_repack.py --bot D:\影刀指令库\指令A.bot --new-version 26.11.2

# 只看包内结构，不改动（不需要 .NET / 影刀）
python bot_repack.py --inspect

# 只看关键字段
python bot_repack.py --dry-run

# 跳过重签（仅改包，用于验证导入层是否校验签名）
python bot_repack.py --no-sign
```

主要参数：

| 参数 | 说明 |
| --- | --- |
| `--scan-dir DIR` / `--batch DIR` | 指定扫描目录，默认 exe 所在文件夹 |
| `--output-dir DIR` | 输出子目录，默认 `repacked` |
| `--bot FILE` | 单文件模式（此时 `--output` 可指定输出路径） |
| `--new-uuid` / `--new-code` / `--new-version` | 覆盖配置区的身份策略 |
| `--old-code` | 显式指定旧指令集编码 |
| `--dotnet PATH` / `--tfm TFM` | 手动指定 .NET 与编译目标框架 |
| `--runtime-dll PATH` | 手动指定 `ShadowBot.Runtime.dll`（最高优先级） |
| `--inspect` / `--dry-run` / `--no-sign` | 诊断与实验模式 |

---

## 五、共享目录部署（多人使用）

把 exe 放到公司共享目录、同事直接在共享目录里运行时，配置区里的 `RUNTIME_DLL` 是你本机的绝对路径，在别人电脑上不存在。脚本按下面顺序自动查找 `ShadowBot.Runtime.dll`，**找到就用；重签失败会自动换下一个候选**：

1. `--runtime-dll` 命令行显式指定
2. **exe 同目录**的 `ShadowBot.Runtime.dll`
3. **exe 同目录 `runtime\` 子文件夹**里的 `ShadowBot.Runtime.dll`
4. 配置区 `RUNTIME_DLL`（你本机的安装路径）
5. 本机 `C:\Program Files\ShadowBot\shadowbot-*\` 自动探测

推荐目录结构：

```
\\公司共享目录\RPA工具\
├── bot_repack.exe
├── ShadowBot.Runtime.dll        ← 方案 A：只拷这一个（可能缺依赖）
├── ShadowBot.Common.dll         ← 若 A 报依赖缺失，把同目录其它 DLL 一起拷
├── ...其它 ShadowBot*.dll
├── runtime\                     ← 方案 B：影刀版本目录里的全部 DLL（最稳）
│   └── ShadowBot.Runtime.dll
├── 指令A.bot
├── repacked\                    ← 自动创建
└── _sigstore_backups\           ← 自动创建（签名备份）
```

> **`ShadowBot.Runtime.dll` 不是孤立文件**：加载时还会解析同目录的其它影刀程序集，以及影刀自带的 .NET 运行时目录。只拷单个 DLL 可能因依赖缺失导致重签失败——这时脚本会自动回退到下一个候选；全部失败时会逐个打印错误并给出提示。
>
> 最可靠的是**方案 B**：把 `shadowbot-5.25.66\` 里的全部 DLL 拷进 exe 同目录的 `runtime\` 子文件夹，依赖齐全，同事电脑通常无需再装影刀客户端。但**仍需 .NET SDK**，因为重签要编译临时 C# 程序。
>
> 脚本会把所有候选目录及其 `dotnet.win-x64/shared/...` 子目录一并作为程序集探测路径传给重签程序，所以「同目录 DLL + 本机影刀安装目录」混合使用也能解析到依赖。
>
> 共享目录需要给使用者**写权限**（要创建 `repacked\` 与 `_sigstore_backups\`）。

---

## 六、处理流程

1. 解包 `.bot`（本质是 zip），定位 `xbot_robot/package.json`。
2. 生成新 `uuid`，并在**包内所有文本文件**中替换旧 uuid（`settings.json`、`xbot_extensions/` 等处也有引用）。
3. 自动识别 `activity_xxx` 指令集编码并替换，同步重命名含编码的目录/文件名；二进制流程文件（`.pybx` 等）跳过，不破坏内容。
4. 版本号末位 +1。
5. 加载影刀自带的 `ShadowBot.Runtime.dll`，调用 `PackageHelper.WritePackageSigstore()` 重新生成有效签名（**不是伪造签名**，用的是影刀自己的 API），并校验 `after=True`。
6. 重新打包为 `.bot`。

已针对影刀 5.25.66 的 4 参 `Package` 构造函数 `(String rootPath, PackageAppInfo appDetail, bool? isSync, String targetPath)` 做适配；较新版本的 5 参构造也兼容，其余版本会反射枚举构造函数。

---

## 七、注意事项

- **合规**：仅用于处理你自己或你公司有权分发的指令。绕过产品约束前请确认不违反适用的许可协议。
- **更省事的官方通道**：同一企业组织内分发，推荐走「我开发的指令 → 发版 → 分享至企业 → B 账号在『企业指令』安装」，天然支持一个指令集多账号安装，不需要生成多份文件。本工具适合跨组织、离线交付等场景。
- 重签依赖 .NET SDK 与影刀客户端；缺少任一项时可用 `--no-sign` 先验证导入层是否校验签名。
- 改 `NEW_CODE` 会影响 `prototype.block.json` 等处；若发现指令调用异常，可先 `--inspect` 查看编码位置，或把 `NEW_CODE` 改为 `None` 只换 uuid。
- `.gitignore` 已排除 `*.exe`、`build/`、`dist/`、`*.spec`、`repacked/`、`_sigstore_backups/`、`*.bot` 等，避免误提交二进制与签名备份。
