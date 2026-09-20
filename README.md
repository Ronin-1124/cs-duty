# cs-duty

客服值班台（Python 包名为 `cs_duty`）。Windows 本机运行：电商后台客户端经 **Radxa Linkr**（HDMI 截图 + USB HID）读写会话，LangGraph 编排业务，MiniMax 生成方案，SQLite 保存客户上下文，飞书对接审阅同事与知识 agent。

**只做三件事：** 真人账号首应防扣费；收集需求推飞书同事并双向收回；咨询题问飞书知识 agent，改写成对客短回复（机密则转人工）。打单、退款等后台 GUI 一律交给同事。不操作任何网页，全部经客户端截图与 HID。

## 启动

Windows 10/11 x64，联网执行：

```powershell
.\setup.cmd
.\run.cmd serve
```

`setup.cmd` 安装项目依赖；没有项目虚拟环境时，通过 [uv 官方安装器](https://docs.astral.sh/uv/reference/installer/)下载项目内的 Python 3.12，无需预装 Python。下载的运行时位于 `artifacts/runtime/`，不修改系统 PATH。第一次安装需要访问 Astral、GitHub 和 Python 包源。

运行 `run.cmd serve` 以启动服务。

- 客服管理：<http://127.0.0.1:18766/manage>

服务只监听本机。按 Ctrl+C 停止服务及其客户端连接；正在进行的模型请求会等待结束，默认超时 40 秒。端口已占用时拒绝启动第二个实例。也可使用 `run.cmd serve --port 18767` 或 `--data-dir artifacts/another-shop`。

其他系统可以自行建立 Python 3.12 虚拟环境并安装 `requirements.txt`，再执行 `python -m cs_duty serve`；尚未完成跨平台部署验证。

## 第一次接待

1. 打开“模型配置”，保存 API 协议、地址、模型名和密钥，测试连接。首次启动会从项目 `.env` 中的 `MINIMAX_*` 配置导入一个 MiniMax 连接，之后以管理页面保存的配置为准。
2. 在“业务知识”同步项目资料或添加知识。优先使用 `artifacts/knowledge/knowledge-cleaned/` 整理包；没有整理包时，首次启动从 `data/raw/` 导入客服 CSV。相同整理包重复同步不会新增，不修改原始材料。
3. 在“接待设置”选择回复方式（填写草稿或自动发送），保存 Linkr 地址与 Token（可留空使用 `.env` 中的 `LINKR_TOKEN`）。首次使用前需按 `experiments/dongdong_slots.json` 完成槽位标定，并用 `run.cmd observe-desktop` 自检。
4. 真实接待：全屏打开电商后台客户端（如咚咚、千牛），Linkr 已用 USB-C 连接。点击“开始接待”。
5. “填写草稿”经 Linkr 粘贴到客户端输入框，**不点发送**。需要发出去时由同事在客户端发送，或打开自动发送开关（需先标定发送槽位）。
6. 定制需求按字段逐步收集；报价先收集型号、数量、联系渠道（可选择当前会话），收齐后进入“同事待办”。其他需要核实的问题使用“我帮您看看，稍等。”回应。提交处理结果后，正在运行的客服继续生成回复。会话支持“同事接管”和“恢复接待”。

## 模型连接

当前默认模型为 `MiniMax-M3`，使用国内 API。已实测以下两种配置可用：

| 协议 | API 基础地址 | 程序追加路径 |
| --- | --- | --- |
| Anthropic Messages | `https://api.minimax.cn/anthropic` | `/v1/messages` |
| OpenAI Chat Completions | `https://api.minimax.cn/v1` | `/chat/completions` |

管理页面允许保存和切换多个兼容接口；填写基础地址，不要重复填写完整请求路径。MiniMax-M3 请求关闭 thinking，仅使用最终答复。不同供应商的私有扩展不保证兼容。

业务角色固定为“店铺客服同事”，协作时称“同事”“负责售后的同事”，不用“转人工”等口吻。不虚构查询结果、实际库存、承诺或未执行的转交；模型按业务意图输出结构化方案，由程序检查依据、需求字段和发送状态。

密钥保存在本机 `.env`（可选导入）和 `artifacts/app/business.sqlite3`，管理 API 不返回明文密钥。当前是本机文件存储，尚未接入 Windows 凭据管理器。这些数据以及原始 CSV 都被 Git 忽略。迁移时停止应用，再复制需要保留的数据目录；不要复制 `.venv`，在新机器重跑安装。

## 同事协作与边界

- 飞书支持自定义机器人 Webhook 和可选签名密钥，需在接待设置中显式启用。生成待办后发送一次通知；通知状态持久化，结果不明不会盲目重发。同事目前在本机管理页填写处理结果，尚未实现飞书卡片回调或跨机器远程管理。飞书接口用替身测试验证，未向真实机器人发送测试通知。
- 原始聊天、已收集字段、待办与发送队列持久化；LangGraph 使用独立 SQLite 检查点保存等待状态。重新启动后可继续处理，但启动服务不会自动开始接待。
- 新消息会使旧草稿过期；同事接管会阻止自动发送；发送确认丢失的回复标记为“待核对”，需核对客户端后处理。
- 无关请求引导回业务。首次致谢回复“客气了，有需要随时联系我。”，之后连续客套不重复回应；客户有新问题时正常处理。重复无关请求和常见结束语不反复回答。此版本没有按客户计费、每日配额或完整反滥用评分。
- 当前知识检索使用 SQLite FTS5、中文双字词片段、业务关键词扩展及型号别名过滤，没有向量库；历史保存完整，但单次模型上下文使用最近 60 条文字消息、最多 32000 字符及最近检索资料，不是无限上下文。

## 飞书协作

支持单向 Webhook 通知及应用机器人双向协作。双向接入使用长连接，按员工与会话白名单接收待办结果，再由程序按所选回复方式继续处理。首次配置、绑定本人和测试步骤见[飞书协作文档](docs/feishu.md)。

## 客户数据与迁移

客户详情支持清空聊天、删除客户；“数据管理”提供全部客户清理和 ZIP 导出。迁移恢复命令为 `run.cmd restore-data <迁移包.zip> --data-dir <新目录>`。操作范围、密钥选项及迁移步骤见[数据管理文档](docs/data-management.md)。

## 整理后的知识包

停止服务后执行 `run.cmd import-knowledge artifacts/knowledge/knowledge-cleaned`，也可停止接待后在“业务知识”点击“同步项目资料”。命令支持 `--data-dir` 指定店铺数据目录。

导入会检查 JSONL 格式、稳定 ID、CSV 行追溯和官方提交一致性，原子替换知识包；旧三份 CSV 的直接回复条目会停用，手动知识、模型配置与聊天记录保留。同一包重复导入不改变条目开关，更新包也保留已停用的官方条目。

官方正文仅在有产品范围、可独立使用且没有解析问题时启用，按完整章节保留前提、表格、代码及产品变体条件。中文优先，同页无可用中文时使用明确标注的英文来源。过长章节保留在原始资料中待复核，不截断后交给模型。来源中的图片尚未读取。

CSV 清洗行、问答候选、店铺规则、别名、核实事项、官方原文和整理说明全部存入本地数据库，可通过“查看资料”选择查看。问答的核对标签不等于整段回答全部可靠，自动回复使用官方原文依据；候选结论与风格示例不会直接作为事实。新增业务规则可由同事核实后通过“添加知识”录入。

数据包、数据库和导入报告位于被 Git 忽略的 `artifacts/`，迁移到另一台机器时需另行复制，代码仓库不包含业务原始资料。

## 桌面客户端接入（Linkr）

网页自动化路径已放弃（风控）。唯一通道是桌面客户端（默认进程 `jdm_dd_workbench`）+ Linkr：

- 截图：`GET /api/public/snapshot`（处理 16:9 黑边后映射窗口）
- 点击/粘贴：`POST /api/public/control` USB HID
- 识别：Windows OCR（离线）读取会话列表、聊天记录与输入框；面板矩形与槽位见 `experiments/dongdong_slots.json`
- 多客户端：接待设置中的"槽位表路径"可切换客户端，千牛接待台使用 `experiments/qianniu_slots.json`（`process: AliWorkbench`，按窗口标题 `千牛接待台` 定位）
- 标定：`run.cmd observe-desktop` 输出带标注的截图与 OCR 结果，用于校正槽位
- 默认不点「发送」，需显式开启并先标定发送槽位

未标定的槽位（截图中 `null` 的项）会让相关操作返回明确错误，不会盲目操作。详见 [产品边界](docs/product.md) 与 [架构设计](docs/architecture.md)。

## 代码结构与验证

- `cs_duty/`：应用服务、业务存储、模型协议、知识导入、LangGraph 流程、管理页面。
- `cs_duty/desktop/`：Linkr HID、窗口聚焦、剪贴板、槽位表加载、Windows OCR 与桌面适配器。
- `deploy/setup.ps1`：Windows 环境安装实现，统一由根目录 `setup.cmd` 调用。
- `tests/`：单元测试，不依赖真实客户资料或真实设备。
- `data/raw/`：原始客服 CSV，命名与来源对应关系见 [资料说明](docs/source-data.md)。CSV 不提交到 Git。
- `docs/`：项目文档，包括[业务需求](docs/requirements.md)、[架构设计](docs/architecture.md)、[数据管理](docs/data-management.md)和[飞书协作](docs/feishu.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

桌面适配器测试使用注入的假截图、假 OCR 和假 Linkr 客户端，可在不连接设备时运行。

离线 Lucide 图标库许可证见 `cs_duty/web/lucide.LICENSE`。
