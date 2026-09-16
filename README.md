# cs-duty

客服值班台（Python 包名为 `cs_duty`）。Windows 本机运行：桌面咚咚经 **Radxa Linkr**（HDMI 截图 + USB HID）读写会话，LangGraph 编排业务，MiniMax 生成方案，SQLite 保存客户上下文，飞书对接审阅同事与知识 agent。

**只做三件事：** 真人账号首应防扣费；收集需求推飞书同事并双向收回；咨询题问飞书知识 agent，改写成对客短回复（机密则转人工）。打单、退款、京麦后台等 GUI 一律交给同事。不用京东网页 DOM。

## 启动

Windows 10/11 x64，联网执行：

```powershell
.\setup.cmd
.\run.cmd serve
```

`setup.cmd` 安装项目依赖；没有项目虚拟环境时，通过 [uv 官方安装器](https://docs.astral.sh/uv/reference/installer/)下载项目内的 Python 3.12，无需预装 Python。下载的运行时位于 `artifacts/runtime/`，不修改系统 PATH。优先使用已安装的 Edge；没有 Edge 时安装 Chromium，需要在接待设置中选择 Chromium。第一次安装需要访问 Astral、GitHub 和 Python 包源。

当前机器已经安装好依赖，直接运行 `run.cmd serve` 即可。

- 客服管理：<http://127.0.0.1:18766/manage>
- 模拟客户：<http://127.0.0.1:18766/control>
- 模拟工作台：<http://127.0.0.1:18766/workbench>

服务只监听本机。按 Ctrl+C 停止服务及其接待浏览器；正在进行的模型请求会等待结束，默认超时 40 秒。端口已占用时拒绝启动第二个实例。也可使用 `run.cmd serve --port 18767` 或 `--data-dir artifacts/another-shop`。

其他系统可以自行建立 Python 3.12 虚拟环境，安装 `requirements.txt` 和 Playwright Chromium，再执行 `python -m cs_duty serve`；尚未完成跨平台部署验证。

## 第一次接待

1. 打开“模型配置”，保存 API 协议、地址、模型名和密钥，测试连接。首次启动会从项目 `.env` 中的 `MINIMAX_*` 配置导入一个 MiniMax 连接，之后以管理页面保存的配置为准。
2. 在“业务知识”同步项目资料或添加知识。优先使用 `artifacts/knowledge/knowledge-cleaned/` 整理包；没有整理包时，首次启动从 `data/raw/` 导入客服 CSV。相同整理包重复同步不会新增，不修改原始材料。
3. 在“接待设置”选择回复方式（填写草稿或自动发送）。本地模拟工作台仅用于业务流程回归，不连接京东。
4. 真实接待：全屏打开桌面咚咚，Linkr 已用 USB-C 连接并在 `.env` 配置 `LINKR_TOKEN`。点击“开始接待”。
5. “填写草稿”经 Linkr 粘贴到咚咚输入框，**不点发送**。需要发出去时由同事在客户端发送，或打开自动发送开关（单独评审）。
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
- 新消息会使旧草稿过期；同事接管会阻止自动发送；发送确认丢失的回复标记为“待核对”，需核对网页后处理。
- 无关请求引导回业务。首次致谢回复“客气了，有需要随时联系我。”，之后连续客套不重复回应；客户有新问题时正常处理。重复无关请求和常见结束语不反复回答。此版本没有按客户计费、每日配额或完整反滥用评分。
- 当前知识检索使用 SQLite FTS5、中文双字词片段、业务关键词扩展及型号别名过滤，没有向量库；历史保存完整，但单次模型上下文使用最近 60 条文字消息、最多 32000 字符及最近检索资料，不是无限上下文。

## 飞书协作

支持单向 Webhook 通知及应用机器人双向协作。双向接入使用长连接，按员工与会话白名单接收待办结果，再由 RPA 按所选回复方式继续处理。首次配置、绑定本人和测试步骤见[飞书协作文档](docs/feishu.md)。

## 客户数据与迁移

客户详情支持清空聊天、删除客户；“数据管理”提供全部客户清理和 ZIP 导出。迁移恢复命令为 `run.cmd restore-data <迁移包.zip> --data-dir <新目录>`。操作范围、密钥选项及迁移步骤见[数据管理文档](docs/data-management.md)。

## 整理后的知识包

停止服务后执行 `run.cmd import-knowledge artifacts/knowledge/knowledge-cleaned`，也可停止接待后在“业务知识”点击“同步项目资料”。命令支持 `--data-dir` 指定店铺数据目录。

导入会检查 JSONL 格式、稳定 ID、CSV 行追溯和官方提交一致性，原子替换知识包；旧三份 CSV 的直接回复条目会停用，手动知识、模型配置与聊天记录保留。同一包重复导入不改变条目开关，更新包也保留已停用的官方条目。

官方正文仅在有产品范围、可独立使用且没有解析问题时启用，按完整章节保留前提、表格、代码及产品变体条件。中文优先，同页无可用中文时使用明确标注的英文来源。过长章节保留在原始资料中待复核，不截断后交给模型。来源中的图片尚未读取。

CSV 清洗行、问答候选、店铺规则、别名、核实事项、官方原文和整理说明全部存入本地数据库，可通过“查看资料”选择查看。问答的核对标签不等于整段回答全部可靠，自动回复使用官方原文依据；候选结论与风格示例不会直接作为事实。新增业务规则可由同事核实后通过“添加知识”录入。

数据包、数据库和导入报告位于被 Git 忽略的 `artifacts/`，迁移到另一台机器时需另行复制，代码仓库不包含业务原始资料。

## 真实桌面接入（Linkr）

京东网页 Playwright 路径已放弃（风控）。真实通道是桌面 `jdm_dd_workbench` + Linkr：

- 截图：`GET /api/public/snapshot`（处理 16:9 黑边后映射窗口）
- 点击/粘贴：`POST /api/public/control` USB HID
- 控件：`experiments/dongdong_slots.json` 预录相对坐标与面板矩形；列表项在会话区内 OCR
- 默认不点「发送」

本地模拟工作台仍可用于不连京东的业务回归。详见 [产品边界](docs/product.md)。

## 代码结构与验证

- `cs_duty/`：应用服务、业务存储、模型协议、知识导入、LangGraph 流程、管理页面。
- `cs_duty/desktop/`：Linkr HID、窗口聚焦、剪贴板粘贴。
- `deploy/setup.ps1`：Windows 环境安装实现，统一由根目录 `setup.cmd` 调用。
- `mock_dongdong/`：本地复刻网页、隔离测试数据、旧模板 DOM 回归；不会连接京东。
- `tests/`：单元测试与可选端到端测试，不依赖真实客户资料。
- `data/raw/`：原始客服 CSV，命名与来源对应关系见 [资料说明](docs/source-data.md)。CSV 不提交到 Git。
- `docs/`：项目文档，包括[业务需求](docs/requirements.md)、[架构设计](docs/architecture.md)、[模拟回归](docs/offline-demo.md)和[真实页面采集记录](docs/jingmai-dom-observations.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m tests.service_smoke
.\.venv\Scripts\python.exe -m tests.service_smoke --live-model
.\run.cmd demo
```

`service_smoke` 使用临时数据库和合成客户，通过浏览器测试管理页、审核发送、暂停恢复、定制协作、移动端布局；默认替代模型，不调用外部 API。`--live-model` 只将硬编码的虚构 TEST-1 资料和合成对话发给 `.env` 指定的模型，不导入项目 CSV。截图和报告位于 `artifacts/service-smoke/` 或 `artifacts/service-live-smoke/`。

离线模板回归详情见 [模拟回归文档](docs/offline-demo.md)，真实 DOM 依据见 [页面采集记录](docs/jingmai-dom-observations.md)。回归自行创建的服务会随测试结束关闭。

离线 Lucide 图标库许可证见 `mock_dongdong/web/lucide.LICENSE`。
