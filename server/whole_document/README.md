# 可选的整篇 PDF 翻译服务

这是独立运行的实验性服务，提供整篇上下文、按章节分块、局部修复和费用追踪。
现有 `server/server.py`、插件和原服务端口不变。上游尚未合并本功能。

## 安装与启动

需要 Python 3.12、Git 和 [uv](https://docs.astral.sh/uv/)。在本目录执行：

```sh
uv sync --locked
uv run python -m unittest discover -s tests -q
uv run python worker/run.py --check
```

`uv.lock` 固定全部 Python 依赖和 BabelDOC fork 的完整 commit。
不需要复制本机安装目录，也不需要全局替换 BabelDOC。

将 `config/channel.example.json` 复制到本目录下的 `private/channel.json`，
编辑服务地址、模型和密钥。默认配置故意不可发起付费请求：必须明确填入实际
输入/输出容量、`capacity_verified`、输入/输出单价和 `max_cost` 后才就绪。
容量需经通道验证，不能仅根据模型名字猜测。不要提交 `private/`、`runs/`
或运行日志。

```sh
uv run python scripts/service.py start --runtime runtime --config private/channel.json --runs runs
```

打开 <http://127.0.0.1:8891/>。同一组参数将 `start` 改为 `status` 或 `stop`
可检查或停止本服务。Windows 下后台启动不弹出控制台。
网页刷新、健康检查、进程重启均不会自动发送翻译请求。

命令行处理单篇 PDF：

```sh
uv run python worker/run.py input.pdf --config private/channel.json --runs runs
```

仅在主动选择恢复时添加 `--resume`。未知结果不代表零费用，也不会自动重发
整篇或退回旧引擎。每篇的请求、token 和费用预算跨进程持久保存。

## 翻译与验证规则

- 可容纳时整篇一次调用，超限按章节分组；可显式设置 `strategy: "chapters"`。
- 有效译文同时用于单语和双语输出；修复只发送未通过项，最多两轮、每段两次，
  累计修复原文量不超过 20%。
- 检查段落 ID、缺失/重复、公式占位符、样式标记和疑似漏译。
- `preserve_terms` 只精确匹配独立名称标签；正文中出现同名词仍会翻译。
- `proxy_mode` 可选 `system`、`environment`、`direct`，两个 HTTP 实现使用
  同一显式策略；不修改系统代理，回环地址直连。
- 共享熔断、取消、检查点、请求去重、费用预留与 API usage 分别记录。
- `complete` 才映射为旧插件的成功；`partial` 文件有明确标记，旧插件不能自动挂回。
  可从新监控页下载部分文件和 QA 清单。
- 日志只记录脱敏网络阶段、异常链和允许的响应头，不记录请求正文或 Authorization。

费用按所填单价估算，不能代替通道账单。默认不假设缓存折扣。输出上限和
实际 usage 的计量可能因供应商而不同，应保留预算余量。

## 无付费调用的验证

```sh
uv run ruff check translation worker server/whole_app.py tests scripts
uv run python -m unittest discover -s tests -q
uv run python scripts/synthetic_pdf.py runs/synthetic
uv run python scripts/integration_http.py runs/http runs/synthetic/synthetic-source.pdf
```

后两条使用本地构造响应或本地假模型，覆盖真实 PDF 引擎、后台 worker、HTTP
服务、完整/部分/无通道结果和刷新/重启不派发。引擎首次使用可能下载布局/字体
资产；这些下载不产生模型翻译费用。HTTP 集成使用本地端口 8893。

## 实验记录与边界

在此前 BabelDOC 0.6.2 补丁环境中，一篇 13 页 DOSA 论文通过了 1 次整篇请求和
1 次仅 4 段修复：18,818 输入 + 19,111 输出 = 37,929 token。
使用该次通道确认价格估算人民币 0.02973698 元。单语 13 页、交替双语 26 页，
197 个翻译项、147 个保留项，结构校验和页面渲染复查通过。

这不是与旧流程严格配对的节省实验，不保证其他通道的价格或长请求稳定性。
本分支已移植到 BabelDOC 0.6.4 fork，需以本分支的测试/CI 记录判断移植验证；
没有把旧环境的一次真实模型成功冒充新依赖环境的付费验收。
本分支在独立 Python 3.12.15 环境中 `uv sync --locked` 后，34 项测试、
ruff、真实引擎合成 PDF 和后台 HTTP 集成均通过。测试复用了已有引擎资产缓存，
不是空缓存机器验收。依赖锁避开了已确认不兼容的 bitstring 5 接口变更，
并限制相关主版本升级，更新依赖后必须重新运行验证。
代表译文需要作者人工确认，Zotero 现场附件挂回、整篇/章节冷缓存对照和生产
端口切换尚未验收。不要直接替换生产入口。

## 维护与上游

Zotero PR 分支为 `feat/whole-document-translation`，本 fork 的持续维护分支为
`whole-document`。BabelDOC 扩展在
[sitonwall/BabelDOC](https://github.com/sitonwall/BabelDOC/tree/whole-document)，
当前依赖固定到 `f85f85ab043be3f998e5f55a91d50bd2fe391ab2`。
独立排版修复已提交 [BabelDOC #629](https://github.com/funstory-ai/BabelDOC/pull/629)。

更新依赖时先创建分支、更新固定 commit 与 `uv.lock`，再运行上面的离线和真实
HTTP 检查。上游若不接受实验性服务，本 fork 会继续保留独立入口和明确版本。
本功能遵循仓库根目录的 AGPL-3.0 许可证；原作者和依赖的许可证保持不变。
