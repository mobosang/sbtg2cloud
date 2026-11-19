# 接口证据与实现边界

核实日期：2026-10-19。没有复制第三方库代码到产品，`.research` 仅是本地调研缓存，不包含在 Docker 镜像中。

## Telegram

- [官方申请应用凭据](https://core.telegram.org/api/obtaining_api_id)
- [官方扫码流程](https://core.telegram.org/api/qr-login)
- [Telethon qr_login 与两步验证](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.auth.AuthMethods.qr_login)

使用单个用户会话，`catch_up=False`，关闭 SDK 自动重连。连接和鉴权检查完成后才开启接收；初始化失败后即使底层连接仍打开，也会先断开再重新初始化。启用聊天的 NewMessage、MessageEdited 与在线补查共用正文、文本超链接和 URL 按钮提取及去重流程。补查时间窗以在线起点、启用时间和最近 5 分钟中最晚者为准，不回扫旧消息或补抓离线消息。回调按钮和需要机器人交互的链接不自动操作。日志仅记录消息编号、来源、处理入口、入队和去重结果，不记录正文、分享 URL 或凭据。

## 光鸭

主证据为[官方当前前端](https://www.guangyapan.com/static/js/index.b1e7c5bb.js)；交叉参考 [ChenyangGao/guangyapan](https://github.com/ChenyangGao/guangyapan/blob/main/guangyapan/client.py) 和 [DDSRem-Dev/guangyaclient](https://github.com/DDSRem-Dev/guangyaclient/blob/master/guangyaclient/client.py)。其中旧版第三方字段与当前网页不同，以官方网页为准。

| 功能 | POST 地址/参数 | 已核实返回/行为 |
|---|---|---|
| 扫码 | `account.guangyapan.com/v1/auth/device/code`，`client_id`、`scope=user` | `device_code`、`verification_uri_complete`、`expires_in`、`interval` |
| 授权 | `account.guangyapan.com/v1/auth/token`，device_code grant | 未授权时 HTTP 400 + `authorization_pending`，不是终止错误 |
| 续期 | 同 token 接口，refresh_token grant | 更新并保存 access/refresh token |
| 列目录 | `api.guangyapan.com/userres/v1/file/get_file_list` | `parentId`，page 从0开始；`data.list`；`resType=2` 为目录 |
| 分享令牌 | `/userres/v1/get_share_access_token` | `{shareId, code}` → `data.accessToken` |
| 分享总大小 | `/userres/v1/get_share_summary` | `{shareId, needFileNum:false}` → `data.totalFileSize`，单位字节，包含子目录 |
| 分享列表 | `/userres/v1/get_share_page_files_list` | `accessToken,parentId,pageSize,cursor`；`data.list,hasMore,cursor` |
| 转存 | `/userres/v1/restore_share` | `accessToken,fileIds,parentId` → `data.taskId` |
| 进度 | `/userres/v1/get_task_status` | `taskId`；`data.status=2` 完成，`3` 失败，终态 `detail.code!=0` 同样失败 |

匿名验证完成：扫码请求正常返回；未扫码轮询返回 HTTP 400 / authorization_pending。未将匿名设备码或二维码保存到项目。

分享大小限制（2026-11-17）：[官方 Drive Skill 1.0.2](https://gyp.guangyapan.com/guangyapan_drive/guangyapan-drive-1.0.2-bundled.zip) 的 `getShareSummary` 使用上述摘要接口；已通过部署环境只读请求确认 `totalFileSize` 为整数字节数。监听和手动任务在转存前共用此检查，不调用下载准备接口，不逐层遍历分享。WebUI 的上限以正整数 GB 保存为 SQLite 设置 `share_size_limit_gb`，默认 200；提交前再核对当前上限。超限标记 `discarded`，大小字段缺失或异常则停止转存；已取得云端回执的任务仍只轮询原任务。限制依赖光鸭返回的摘要，不代表实际下载流量或后续 FFprobe 消耗。

用户部署环境只读诊断确认（2026-10-19）：普通空目录的 `file/get_file_list` 返回 HTTP 200，`data` 为 `{}`，没有 `total` 或 `list`；非空目录返回 `data.total` 和 `data.list`。仅对普通目录列表接受空对象，不推广到分享游标接口或未知的非空响应结构。已加入空子目录递归核对及旧临时空目录重试的回归测试。

真实账号尚待验证：扫码授权成功、授权续期、分享提取码、文件夹全量转存、失效链接/空间不足、服务端同名冲突。后台状态未知或回执缺失时不判成功。

## 删除接口（2026-10-19）

[官方 OpenAPI v1.3 文档](https://ossdata.guangyapan.com/doc/guangyapan_openapi_cn.md) 未列出网盘文件删除 API；`delete_upload_task` 是取消上传任务，不能代替文件删除。文档中的 `get_info_by_task_id` 用于上传结果，不能假设返回分享转存后的目标 ID。

官方网页 JS 的删除调用及 [ChenyangGao/guangyapan 的 fs_delete](https://github.com/ChenyangGao/guangyapan/blob/main/guangyapan/client.py) 对应 `POST /userres/v1/file/delete_file`，请求 `{"fileIds":["自己网盘的文件或目录ID"]}`，返回 `data.taskId`。普通目录移入回收站；对已在回收站内的对象再次调用可能永久删除。因此实现不自动重试删除请求（包括 401），只轮询已知任务 ID，未知回执人工核对。

转存直接使用用户指定 `parentId`，不再创建中间目录。启用清理时，比较转存前后的目标 ID，并按分享顶层名称、类型、大小定位新增资源；已有同名项、合并或非唯一匹配不自动清理。源分享 ID 不作删除目标。历史缺少成功时间时按任务创建时间判定到期；新旧任务统一使用配置的 5～600 分钟等待时间（默认 60 分钟），但需先选定实际目标 ID。接口仅完成模拟删除测试，没有在开发阶段实际删除账号数据。

新增 `completed_at` 与清理字段通过 SQLite 增量迁移；原记录成功时间保留为空，不伪造。清理状态与转存状态分离，删除失败不修改成功去重记录。`deleting` 启动恢复为 `uncertain`，不会重发；成功后未能记录快照的任务停止清理。

所有光鸭请求共享串行锁与 2 秒间隔；429/5xx 延迟至少 60 秒。清理的年龄筛选每 30 秒仅查本地数据库；目录核对仅在转存完成时和到期后发生。不对“不会封号”作保证。

## 安全与运行模型

### 光鸭取链与 Emby 播放入口（2026-11-16）

依据用户提供的[官方 Drive Skill 1.0.2](https://gyp.guangyapan.com/guangyapan_drive/guangyapan-drive-1.0.2-bundled.zip) 中 `src/api-client.mjs` 的 `downloadUrl(fileId, requestId)`，调用 `POST /userres/v1/get_res_download_url`，参数为 `fileId` 和空 `requestId`，读取 `data.signedURL`。这与现有扫码会话共用令牌和请求限速，不引入 SDK 或其他光鸭登录方式。下载链接只接受 HTTPS 及 `guangyacdn.com` / `guangyapan.com` 域名边界；域名变化时明确报错，不把任意地址作为跳转目标。不能据此承诺客户端的“直链分享”权益、长期有效性或下载速度。

[Emby PlaybackInfo](https://dev.emby.media/reference/RestAPI/MediaInfoService/getItemsByIdPlaybackinfo.html) 声明需要用户认证。播放入口将原客户端凭据传给固定上游，未经成功响应不访问光鸭，不使用管理员密钥替代用户身份。媒体定位仅使用响应中的 MediaSources，`MediaSourceId` 不匹配或版本不唯一时留给上游处理；不接受客户端自报的光鸭文件 ID。[Emby 视频流接口](https://dev.emby.media/reference/RestAPI/VideoService/getVideosByIdStream.html) 中 `Static=true` 表示无编码的原始文件，本实现据此限制重定向范围，HLS 和烧录字幕不强制跳转。SenPlayer 为本阶段目标客户端，其他客户端未经现场验证不宣称兼容。

新增的 `media_proxy` 设置通过现有 settings 表保存，不修改任务表、监听、转存或清理语义。可选的第二 Uvicorn 监听器运行在主进程同一事件循环，复用现有 Store 和 Cloud，避免多实例读取 Telegram 会话。WebSocket 仅用于转发 Emby 控制消息，使用 `websockets==15.0.1`，显式关闭环境代理和协议调试日志。

附加入口使用单独的 `media_proxy_secondary` 设置与 `8082` 监听端口，沿用同一 `MediaProxy` 实现，不创建第二个 Store、Cloud、Worker 或 Telegram 会话。两个代理共享取链锁及持久缓存，分别持有上游 HTTP 客户端、目录内存缓存、计数和错误。设置接口同时拒绝指向本服务两个播放端口的明显循环；添加现有反代为上游前仍需核对它的实际后端，避免间接循环。原上游返回 401/403 时不访问缓存直链或绕过鉴权。部署验证需要使用该上游认可的客户端会话，管理员 API Key 被拒绝时不把它当作普通网络故障。

HTTP 请求和响应头以原始字节转发，避免中文设备名等 UTF-8 值被默认 ASCII 编码拒绝或二次编码。WebSocket 握手将有效 UTF-8 请求头交给库序列化，非法编码关闭连接，不记录原始头。错误只保留经过处理的说明及异常类型，不回显请求 URL、认证头或第三方异常内容；上游鉴权拒绝单独显示 HTTP 状态。

只转发当前请求自带的 Cookie；没有 Cookie 时显式提供空头，阻止共享 HTTP 客户端将先前登录响应中的上游会话 Cookie 带给其他用户。登录响应的 Set-Cookie 原样交给原请求客户端。

现场接口只读验证：复用已有扫码令牌，为一部既有影片获取 `signedURL` 成功；CDN 对 `bytes=0-1023`、`bytes=100000-101023` 均返回 HTTP 206 及正确 Content-Range，每次仅读取 1 KiB。未改动云盘文件或转存任务。这证明取链和分段接口可用，不代表 SenPlayer 设备端起播、持续速度或所有客户端已验证。

### 随直链有效期保留的缓存

`media_links` 是独立的 SQLite 缓存表，不改动既有转存或删除回执。缓存使用登录会话的随机归属标识和云盘路径作为唯一键；获取时间、共用的到期时间、命中次数及最近使用时间随记录持久化。可复用截止时间按 `urlDuration` 所声明的秒数计算，保留 5 秒余量，记录的 `retained_until` 与 `expires_at` 相同，不再套用固定的一天期限。升级时将已有记录的保留时间同步到原直链的到期时间，URL、命中次数和登录会话不变；不为旧记录重新签发或延长直链。SQLite 表内保存完整签名 URL，管理 API 只选择元数据列。

二维码授权通过 `save_cloud_token(..., new_session=True)` 在同一事务内写入令牌、轮换缓存归属并清空旧记录；正常刷新令牌保留归属。已有数据库首次启动时补充归属标识，不更换原令牌。缓存读取和刷新串行合并；取链期间若会话变化，不保存或返回旧会话取得的 URL。每次跳转前仍使用原客户端凭据向 Emby 查询播放信息，不从缓存推断用户权限。清空缓存与在途取链共享锁，不会把刚清空的旧请求结果重新写回。

列表分页、命中统计、超期淘汰和手动清空只操作本地 SQLite，不调用光鸭或探测 CDN。多用户复用仅说明服务端返回相同的有效签名地址，不代表光鸭承诺跨 IP、设备或无限并发可用，也不改变流量计费规则。

所有云盘 HTTP 请求固定发往 account/api.guangyapan.com，不请求消息提供的任意地址；严格校验分享主机和路径，避免服务端任意 URL 访问。管理 API 要求同源及自定义请求头，拒绝跨站请求；页面使用 textContent 展示频道和服务数据，CSP 限制脚本来源。此机制不代替身份认证，局域网其他可访问端口的设备可管理该服务。

单进程 asyncio + 单消费者，SQLite WAL 持久化；`submitting` 在发出写请求前落盘，得到云端 taskId 后切换 `submitted`。仍存在服务端已接受但本地未保存回执的窗口，此时保守转为 `uncertain`，由用户核对，无法承诺跨云服务 exactly-once。

一键清空使用独立的 directory_clears / directory_clear_parts 持久化预览与批次状态，每批最多 100 个目标 ID。保存目录自身不作为删除目标。工作循环共享操作锁，清空与转存、自动删除串行执行；删除提交中断恢复为 uncertain，不自动重发。
