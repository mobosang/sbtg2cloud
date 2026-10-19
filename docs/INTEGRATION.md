# 接口证据与实现边界

核实日期：2026-10-19。没有复制第三方库代码到产品，`.research` 仅是本地调研缓存，不包含在 Docker 镜像中。

## Telegram

- [官方申请应用凭据](https://core.telegram.org/api/obtaining_api_id)
- [官方扫码流程](https://core.telegram.org/api/qr-login)
- [Telethon qr_login 与两步验证](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.auth.AuthMethods.qr_login)

使用用户会话，`catch_up=False`，不调用消息历史读取接口；关闭 SDK 自动重连，由服务管理重连并重置接收时间边界。事件仅处理启用聊天的 NewMessage，正文/文本超链接/URL按钮。消息编辑、回调按钮、机器人交互、需要加入其他群的流程不在第一版范围。

## 光鸭

主证据为[官方当前前端](https://www.guangyapan.com/static/js/index.b1e7c5bb.js)；交叉参考 [ChenyangGao/guangyapan](https://github.com/ChenyangGao/guangyapan/blob/main/guangyapan/client.py) 和 [DDSRem-Dev/guangyaclient](https://github.com/DDSRem-Dev/guangyaclient/blob/master/guangyaclient/client.py)。其中旧版第三方字段与当前网页不同，以官方网页为准。

| 功能 | POST 地址/参数 | 已核实返回/行为 |
|---|---|---|
| 扫码 | `account.guangyapan.com/v1/auth/device/code`，`client_id`、`scope=user` | `device_code`、`verification_uri_complete`、`expires_in`、`interval` |
| 授权 | `account.guangyapan.com/v1/auth/token`，device_code grant | 未授权时 HTTP 400 + `authorization_pending`，不是终止错误 |
| 续期 | 同 token 接口，refresh_token grant | 更新并保存 access/refresh token |
| 列目录 | `api.guangyapan.com/userres/v1/file/get_file_list` | `parentId`，page 从0开始；`data.list`；`resType=2` 为目录 |
| 分享令牌 | `/userres/v1/get_share_access_token` | `{shareId, code}` → `data.accessToken` |
| 分享列表 | `/userres/v1/get_share_page_files_list` | `accessToken,parentId,pageSize,cursor`；`data.list,hasMore,cursor` |
| 转存 | `/userres/v1/restore_share` | `accessToken,fileIds,parentId` → `data.taskId` |
| 进度 | `/userres/v1/get_task_status` | `taskId`；`data.status=2` 完成，`3` 失败，终态 `detail.code!=0` 同样失败 |

匿名验证完成：扫码请求正常返回；未扫码轮询返回 HTTP 400 / authorization_pending。未将匿名设备码或二维码保存到项目。

真实账号尚待验证：扫码授权成功、授权续期、分享提取码、文件夹全量转存、失效链接/空间不足、服务端同名冲突。后台状态未知或回执缺失时不判成功。

## 安全与运行模型

所有云盘 HTTP 请求固定发往 account/api.guangyapan.com，不请求消息提供的任意地址；严格校验分享主机和路径，避免服务端任意 URL 访问。管理 API 要求同源及自定义请求头，拒绝跨站请求；页面使用 textContent 展示频道和服务数据，CSP 限制脚本来源。此机制不代替身份认证，局域网其他可访问端口的设备可管理该服务。

单进程 asyncio + 单消费者，SQLite WAL 持久化；`submitting` 在发出写请求前落盘，得到云端 taskId 后切换 `submitted`。仍存在服务端已接受但本地未保存回执的窗口，此时保守转为 `uncertain`，由用户核对，无法承诺跨云服务 exactly-once。
