---
date: 2026-08-19
number: "032"
title: dev.bat 不加载 .env：凭据环境变量只活在旧进程的会话环境里，给 bat 补 .env 加载又连踩三层 cmd 批处理解析坑
severity: medium
status: resolved
tags: [windows, bat, dotenv, 环境变量, 编码, 一键启动]
module: dev.bat
---

# dev.bat 不加载 .env：凭据环境变量只活在旧进程的会话环境里，给 bat 补 .env 加载又连踩三层 cmd 批处理解析坑

## S · 背景（Situation）

- **项目 / 模块**：`dev.bat` 一键启动脚本（GBK+CRLF，见 STAR #019/#031）与凭据配置链（`tansuo/agent/api_setup.py`、`tansuo/web/app.py` 的 `_effective_cfg`）。
- **环境**：Windows，后端 `python cli.py web`；`demo2/.tansuo/settings.yaml` 的 agent 段用 `${ENV:ANTHROPIC_AUTH_TOKEN:}` / `${ENV:ANTHROPIC_BASE_URL:}` 引用环境变量（项目约定：settings.yaml 不存明文密钥）。
- **当时在做什么**：前一天旧后端进程被结束、新进程重启后，「大模型 API 配置」页显示「当前凭据（未设置），来源：未设置」。排查链：settings 明文 → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` 全空。查注册表（用户级/系统级）确认这两个变量**从来没有持久化过**——此前它们只活在昨天那个旧进程的会话环境里（设置它的终端会话早已关闭），新进程环境里没有，`${ENV:...:}` 默认值为空 → 「未设置」。
- **问题表现**：无报错，属配置恢复问题。症状：配置页凭据来源显示「未设置」，监督 agent 无法调用 LLM。
- **影响范围**：监督 agent 全功能不可用，直到凭据重新进入后端进程环境。
- **复现步骤**：1) settings.yaml 用 `${ENV:...}` 引用；2) 变量只通过临时 `set` 设进某个终端会话；3) 关掉该会话、另起进程 → 引用解析为空。

## T · 目标（Task）

- **要达成什么**：用户选定方案——dev.bat 启动时自动加载项目根目录 `.env`（文件被 gitignore，凭据只留在项目目录里，不持久化到注册表、不进仓库）。配套：入库的 `.env.example` 模板、`.gitignore` 加 `.env`。
- **验收标准**：解析逻辑全用例通过（注释行、空行、`export ` 前缀、键值两侧空格、值带引号、值含 `=`、值含 `&`、空值、无等号行、覆盖既有环境变量的语义）；dev.bat 保持 GBK(936)+CRLF+`chcp 936 >nul`；`git check-ignore` 确认 `.env` 被忽略。
- **约束条件**：凭据不能出现在对话/仓库里（用户自己在本地填写）；不杀正在运行的服务做验证（解析逻辑在仓库外 harness 目录验证）。

## A · 解决方案（Action）

### 排查过程

先在仓库外纯 ASCII 路径 `E:\dotenv_test_tmp` 搭 harness（`.env` 样例 + `test.bat` 解析脚本）逐用例试错。批处理的 `%` 立即展开把「值内容」直接插回命令行重新参与解析，值里一旦带引号/`&` 就会改写命令结构——三层坑全部在这里暴露（报错均为 GBK 原文）：

**坑 1 · 字面引号比较直接炸解析。** 最初用 `if "%V:~0,1%"==""""` 判断值是否以引号开头。当 V=`"quoted value"` 时该行展开成：

```
if """==" " set "V="quoted" & goto trim_v
```

引号配对全乱，cmd 报「此时不应有」，exit 255：

```
此时不应有 " set "V=quoted" & goto trim_v。
```

**坑 2 · `set "V=%V:"=%"` 替换法去引号同样自毁。** 改用「替换掉所有引号」的写法以为能绕开比较，但替换语法里的 `"` 与 `set "..."` 自身定界引号打架：V 为空时该行展开成 `set "V="="`，把 V 赋成了 `"=`（echo 回显实锤），随后 trim 循环里带着引号的 V 继续炸出坑 1 同款错误。带引号非空值同理失败（V 保持带引号进入后续行）。

**坑 3 · call 传参时引号被 cmd 分词撕碎。** 换成 `call :set_env "%%a" "%%b"` 传值、子程序里 `%~2` 剥引号：当 %%b 本身带引号时实参变成 `""quoted value""`，cmd 按引号toggle分词，空格处断开，%2 只剩 `""quoted`——带空格的引号值被截断。至此确认：**任何把「内容可能含引号的值」经立即展开/call 实参传递的写法都不可靠**。

修复后仍遇到两个环境问题：

**坑 4 · 诊断时 echo 输出行被 `&` 劈开。** `echo AMP=[%AMP%]` 展开成 `echo AMP=[foo&bar]`，`&` 之后的 `bar]` 被当命令执行：

```
'bar]' 不是内部或外部命令，也不是可运行的程序或批处理文件。
```

输出诊断本身必须用延迟展开 `echo AMP=[!AMP!]`（执行期插入，不再参与命令分裂）。

**坑 5 · UTF-8+LF 的 .env 让数据行凭空消失。** harness 第一版 `.env` 被写成 UTF-8+LF 换行。cmd 的 `for /f` 按系统 ANSI（GBK）读文件，中文注释的 UTF-8 多字节序列里出现 GBK 前导字节（0x81-0xFE），把行尾 LF（0x0a）当尾字节吞掉，注释行与下一行 `ANTHROPIC_AUTH_TOKEN=...` 粘成一行，整行被 `eol=#` 跳过——症状是该 KEY 永远设不上、静默回退到环境里的继承值。CRLF 安全（CR 即使被吞，行仍由 LF 终结）；纯 ASCII 安全。

另注：harness 目录不能用 `%TEMP%`（`C:\Users\夜月\...` 含非 ASCII 用户名，`cmd /c` 里 `cd` 会静默失败，报 `'test.bat' 不是内部或外部命令`），改用纯 ASCII 路径。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| `if "%V:~0,1%"==""""` 字面引号比较判断并剥引号 | 失败 | 值带引号时展开成 `if """...`，cmd 解析错误「此时不应有」exit 255 |
| `set "V=%V:"=%"` 立即展开替换去引号 | 失败 | 替换语法内的 `"` 与 `set "..."` 定界引号打架，V 为空展开成 `set "V="="` |
| `call :set_env "%%a" "%%b"` + `%~2` 剥引号 | 失败 | %%b 自带引号时 `""quoted value""` 被 cmd 分词撕碎，带空格引号值截断 |
| `echo [%VAR%]` 打印含 `&` 的值做诊断 | 失败 | `&` 把输出行劈成两条命令 |
| 延迟展开 `set "V=!V:"=!"` + 整行走 `ENV_LINE` 变量传递 | 采用 | 解析期不插入值内容，绕开全部引号/`&` 坑 |

### 最终方案

1. **`dev.bat`**（保持 GBK+CRLF）：
   - 顶部加 `setlocal enabledelayedexpansion`（`!...!` 在执行期才插入值，不参与解析期引号/`&` 扫描）。
   - 横幅后、依赖检查前插入加载块（`if exist` + `for /f` 整行读取 + call 子程序）：

     ```bat
     if exist .env (
         echo [提示] 正在加载项目根目录 .env 中的环境变量...
         for /f "usebackq eol=# delims=" %%a in (".env") do (
             set "ENV_LINE=%%a"
             call :set_env
         )
     )
     ```

   - 末尾新增 `:set_env` 子程序（整行在变量里，先整体去引号，再按第一个 `=` 切分，剥 `export ` 前缀、修剪首尾空格，键非空才写）：

     ```bat
     :set_env
     set "ENV_LINE=!ENV_LINE:"=!"
     set "K="
     set "V="
     for /f "tokens=1,* delims==" %%x in ("!ENV_LINE!") do (
         set "K=%%x"
         set "V=%%y"
     )
     if "!K:~0,7!"=="export " set "K=!K:~7!"
     :trim_k
     if "!K:~0,1!"==" " set "K=!K:~1!" & goto trim_k
     if "!K:~-1!"==" " set "K=!K:~0,-1!" & goto trim_k
     :trim_v
     if "!V:~0,1!"==" " set "V=!V:~1!" & goto trim_v
     if "!V:~-1!"==" " set "V=!V:~0,-1!" & goto trim_v
     if defined K set "%K%=%V%"
     goto :eof
     ```

   - 语义约定：`.env` 的值**覆盖**同名环境变量（比 dotenv 常见语义简单、对开发场景直观）；启动的 `cmd /k` 子窗口继承已加载的环境。
2. **`.env.example`**（入库模板）：`ANTHROPIC_AUTH_TOKEN=` / `ANTHROPIC_BASE_URL=` 两行占位 + 格式说明（`KEY=VALUE`、`#` 注释、不给值加引号、支持 export 前缀与值含 `=`、值避免半角感叹号、仅 dev.bat 加载）。
3. **`.gitignore`** 新增 `.env`（`.env.example` 保持入库）；`git check-ignore -v .env` 确认命中。
4. 编码流程沿用项目约定：Write 工具写 UTF-8 → 短 PS 命令规范化 CRLF 并转 936 → GBK 解码回读验证无乱码。

## R · 实际效果（Result）

- **验证方式与结果**：
  - harness 8 用例全绿：注释跳过、`TOKEN=[sk-test-abc123]`（**覆盖**了环境里的继承值）、`URL=[https://api.example.com/v1]`（export 前缀剥除）、`SPACED=[padded value]`（两侧空格修剪）、`QUOTED=[quoted value]`（去引号且保留空格）、`EQVAL=[a=b=c]`（按第一个 `=` 切分）、`AMP=[foo&bar]`（`&` 安全）、无等号行与空值行输出 `[]`，exit 0；
  - dev.bat 实际的嵌套结构（`if exist (...)` 内 `for do (...)` 内 `call`）单独做了集成测试，通过；
  - dev.bat GBK 解码回读全文无乱码；`git check-ignore -v .env` 输出 `.gitignore:24:.env`，忽略生效。
- **前后对比**：原来凭据只存在于某个会话的临时环境，进程一死就丢、无任何持久痕迹；现在 `copy .env.example .env` 填入凭据后，每次 dev.bat 启动自动加载，凭据只留在项目目录、不进仓库。
- **副作用与代价**：
  - 值里的半角 `!` 会被延迟展开吃掉（已在 .env.example 注明；token/URL 不含该字符）；
  - UTF-8 编码 + 仅 LF 换行 + 多字节字符（如中文注释）的 .env 会出现「注释吞掉下一行」的粘连（已在 .env.example 建议常规 CRLF；数据行本身是纯 ASCII 时不受影响）；
  - 直接 `python cli.py web`（不经 dev.bat）不会加载 .env（已注明）。
- **遗留问题与后续**：无。用户已有一份空的 `.env`（由 .env.example 复制），填入凭据后重启即生效。
- **经验教训**：
  1. 批处理里「把内容未知的值经 `%` 立即展开/call 实参/引号比较传递」全是雷区——展开结果会重新参与引号配对与 `&|<>` 命令分裂；延迟展开（`!var!`，执行期插入）是唯一稳的通用解，去引号用 `set "V=!V:"=!"` 这一标准惯用法。
  2. cmd 按系统 ANSI 代码页读文件：非 ANSI 编码的多字节序列里若出现 GBK 前导字节，会把行尾 CR/LF 当尾字节吞掉，造成跨行粘连的灵异现象；给 cmd 读的文件要么纯 ASCII、要么 ANSI 编码 + CRLF。
  3. 给 cmd 用的测试目录避开含非 ASCII 字符的路径（如 `%TEMP%` 下的中文用户名），`cd` 会静默失败且错误表象（'xxx' 不是内部或外部命令）极具误导性。
  4. 诊断含特殊字符的变量值时用 `set VAR` 或 `echo !VAR!`，绝不用 `echo %VAR%`。
