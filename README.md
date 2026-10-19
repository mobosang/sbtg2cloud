# TG → 光鸭 · sbtg2cloud

在 Unraid（amd64）中运行的 Telegram 分享监听与光鸭云盘自动转存服务。中文 Web 管理页，Telegram 用户账号扫码、光鸭扫码、按频道选择目标目录，文件只在云端转存。

## 已实现的行为

- 支持消息正文、隐藏文本超链接、URL 按钮中的 `guangyapan.com/s/…` 分享链接。
- 仅监听勾选的频道和群。首次启动、重启及断线恢复均不扫描历史消息，不补抓离线期间消息。
- 所有已入队任务保存在 SQLite 中；恢复运行后继续处理。
- 转存分享顶层全部文件/文件夹，保留其内部结构。每个来源可指定目录，也可以继承默认目录。
- 分享 ID 全局去重：同链接不会因来自不同频道而重复转存；成功后不追踪分享新增文件。
- 提取码从 URL 参数或同一消息文本识别；失败任务可手动补码和重试。
- 临时读取错误最多尝试 3 次；实际转存请求结果不明时暂停，要求核对云盘后手动重试。
- 提交转存后轮询云端任务，只有确认完成才显示“已完成”。进度查询暂时失败不会重新提交。
- 不发送 Telegram 消息，不点击回调按钮、不访问短链或第三方跳转站。

## Unraid 部署

将本项目源代码放到 Unraid 的一个目录，例如 `/mnt/user/appdata/sbtg2cloud-src`。源码目录和运行数据目录请分开。以下在 **Unraid 终端**执行。

### 方式一：Docker Compose

需要 Unraid 已有 Docker Compose 命令（可通过 Compose Manager 使用）。

```sh
cd /mnt/user/appdata/sbtg2cloud-src
cp .env.example .env
```

编辑 `.env`，将 `BIND_IP` 改为 Unraid 的实际局域网 IP，确认 `APPDATA_PATH`。默认以 Unraid 的 `nobody:users`（99:100）运行。

```sh
install -d -m 700 -o 99 -g 100 /mnt/user/appdata/sbtg2cloud
docker compose up -d --build
docker compose logs --tail=100
```

浏览器打开 `http://你的Unraid局域网IP:8080`。未设置 `.env` 时端口默认绑定所有网卡，建议明确设置 `BIND_IP`。此服务没有管理密码，不做公网端口映射。

### 方式二：Unraid 自带 Docker，无需 Compose

```sh
cd /mnt/user/appdata/sbtg2cloud-src
install -d -m 700 -o 99 -g 100 /mnt/user/appdata/sbtg2cloud
docker build --platform linux/amd64 -t sbtg2cloud:local .
```

在 Unraid Docker 页面“添加容器”，填写：

| 项目 | 值 |
| --- | --- |
| 名称 | `sbtg2cloud` |
| Repository | `sbtg2cloud:local` |
| Network Type | `bridge` |
| WebUI | `http://[IP]:[PORT:8080]` |
| 端口 | 容器 `8080` → 主机 `8080`，TCP |
| 数据路径 | 容器 `/data` → 主机 `/mnt/user/appdata/sbtg2cloud`，读写 |
| Extra Parameters | `--user 99:100 --restart unless-stopped --security-opt no-new-privileges:true --cap-drop ALL` |

镜像包含健康检查。数据目录应只允许服务用户和管理员访问。

## 初始化

1. 打开 [my.telegram.org/apps](https://my.telegram.org/apps)，申请自己的 Telegram 应用，填写 **API ID / API Hash**。这是应用凭据，不是 Bot Token；扫码登录也需要它们。
2. 在管理页保存凭据，生成 Telegram 二维码，通过手机 Telegram 的“设置 → 设备”扫码。如账号启用两步验证，输入密码完成登录。密码不落盘；二维码过期后手动重新生成。
3. 生成光鸭二维码，用光鸭 App 扫码授权。登录令牌会自动续期，失效时重新扫码。
4. 选择默认保存目录；点击“编辑监听 → 获取聊天列表”，勾选频道、配置目录，然后点击“保存监听”。保存后仅显示选中频道，再次点击“编辑监听”可修改；取消会丢弃本次未保存的选择。
5. 等待选中频道发布新的测试分享，在记录中检查“云端处理中 → 已完成”，再去光鸭核对目标目录。

不会回溯旧消息，可在已加入且有发言权限的测试群中发布自己拥有的小文件分享，测试正文、按钮和提取码三种形式。目录选择器选择现有云盘目录；需要新目录时先在光鸭中创建。

转存记录优先显示消息中的资源名称，缺少时使用分享的文件/文件夹名称；一条消息有多个不同分享时分别使用各分享文件名。旧版记录没有保存片名，可点击“补全本页名称”读取原分享名称，此操作不会重新转存；失效或无法访问的分享暂不能补全。

转存记录默认每页 20 条，可选择 10、20、50、100 条，通过上一页、下一页或输入页码跳转。修改每页数量会返回第一页。“最新记录”返回第一页并获取新任务；浏览后续页时固定记录范围，避免新任务插入导致翻页重复或漏项，任务状态仍会刷新。

## 更新现有部署

用新版源码覆盖旧源码目录，保留 `.env` 和原 `/data` 挂载。Compose 部署执行 `docker compose up -d --build`；Unraid 模板部署重新执行 `docker build --platform linux/amd64 -t sbtg2cloud:local .` 后，在模板编辑页面应用配置以重建容器。仅重启旧容器不会换成新镜像。

新版本启动时自动添加资源名称字段，保留原登录、监听配置、任务和去重记录。建议更新前停容器并备份数据目录。

## 数据与恢复

`/data/state.sqlite3` 保存配置、光鸭令牌、来源和任务；`/data/telegram.session` 保存 Telegram 会话。它们是敏感登录凭据，不要上传到 Git、日志或公开聊天。备份前停止容器，并备份整个 `/data`（包含可能的 SQLite WAL 文件）。

任务状态：`queued` 排队、`preparing` 读分享、`submitting` 提交中、`submitted` 云端处理中、`success` 完成、`failed` 失败、`uncertain` 结果待核对。

- 修改目录只影响之后入队的任务；任务保存入队时的目标目录快照。
- 停用来源只停止接收，不取消已入队任务。
- 若在提交请求中途崩溃/超时，恢复后显示“结果待核对”，避免重复转存。检查云盘确定未转存后再重试。
- 云端任务明确失败时，先核对是否部分文件已保存，再手动重试；同名文件如何处理由光鸭服务决定，本项目不会主动删除或覆盖已有文件。
- 退出 Telegram 仅注销本项目会话，并停用来源，不注销手机等其他设备。
- 当前按单 Telegram 账号和单光鸭账号使用；更换光鸭账号前应停止监听并处理完队列，因为现有目录 ID 属于原账号。
- Telegram 消息时间戳精度为秒，启用/恢复监听同一秒的边界消息可能被接收，不扫描历史接口。

## 本地开发及测试

Python 3.12：

```sh
python -m venv .venv
# 激活虚拟环境后
pip install -r requirements.txt
python -m unittest discover -s tests -v
uvicorn app.main:app --host 127.0.0.1 --port 8080 --no-access-log
```

Windows 可使用 `.venv\Scripts\python.exe`。默认本地数据目录 `./data`，可用 `DATA_DIR` 覆盖。必须单 worker 运行，不能启用多个 Uvicorn worker 或同时运行共享 `/data` 的容器。

## 验证范围与限制

已通过模拟协议测试验证分页、令牌续期、去重、队列恢复、重试边界、来源过滤和 Web API 基础防护。已匿名实测光鸭生成二维码与等待授权的 OAuth 响应；实际账号扫码、转存、同名冲突仍须部署后联调。开发环境没有 Docker 引擎，因此本机未构建/启动容器；部署文件以 `linux/amd64` 为目标。

光鸭接口依据当前官方网页前端和公开实现核实，不是官方承诺稳定的第三方 API；发生变化会明确显示失败，不会假报成功。详细证据见 [docs/INTEGRATION.md](docs/INTEGRATION.md)。
