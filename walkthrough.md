# Walkthrough

这份文档记录当前这版 SSL Sync Web 控制台的实现重点，方便合并前快速复盘。

## 1. 总体状态

当前仓库已经覆盖一条完整的可运行链路：

1. 首次进入时完成管理员初始化向导
2. Master 端配置 DNS / WebDAV / Telegram / ACME
3. 通过 Web 控制台管理域名
4. 调用真实 `acme.sh` 申请或续签
5. 上传证书到 WebDAV
6. Node 端通过 API 模式注册、轮询命令、拉取证书并部署
7. 执行结果回传 Master，统一写入任务日志并可由 Master 发送 Telegram

说明：

- Node 拉取证书时默认优先使用 WebDAV
- 如果 WebDAV 不可用、配置为空，或单次下载失败，会自动回退到 Master API 直连拉取证书包

## 2. 前端实现重点

- React + TypeScript + Vite
- 首次安装初始化向导 + 登录前鉴权状态检查
- 路由覆盖 Dashboard / Domains / Nodes / NodeDetail / DNS Channels / Jobs / Settings
- 中英文切换
- Mock 模式支持完整本地联调
- 批量操作与移动端适配已补齐

### 重点页面

#### Domains

- 域名搜索
- 状态筛选
- DNS 渠道筛选
- 批量申请 / 续签 / 同步 / 启用 / 停用 / 删除

#### Nodes / NodeDetail

- 节点注册
- 生成一键接入命令
- 分配域名
- 批量下发 / 批量删除节点本地证书

#### Settings

- WebDAV / Telegram / ACME 设置
- 管理员账号密码修改
- 中英文切换
- 配置下载备份 / 上传恢复

## 3. 后端实现重点

- FastAPI + SQLite
- `app_settings.auth` 持久化管理员初始化状态
- 管理后台接口与 Node API 接口分离
- 真实 DNS / WebDAV / Telegram 测试
- 首次初始化接口：
  - `/api/auth/status`
  - `/api/auth/bootstrap`
  - `/api/auth/login`
- 域名批量动作接口 `/api/admin/domains/bulk-action`
- 备份导出 `/api/admin/backup`
- 备份恢复 `/api/admin/backup/restore`
- Node command queue + ack/report

## 4. Node 执行模型

Node 端由两层脚本组成：

- `cert-node-agent.sh`
  - 负责心跳、拉 assignments、轮询 commands、ACK 执行结果
- `cert-node-pull.sh`
  - 负责真实下载证书、校验、部署、服务校验、重载
  - 下载策略为 WebDAV 优先，Master API 回退

当前采用 `systemd timer` 每 2 分钟轮询一次的近实时模式，不是服务端主动推送。

## 5. Telegram 行为

### Master 端

- 单域名动作：按任务发消息
- 批量域名动作：合并成一条汇总消息

### Node API 模式

- Node 本地不再直接发 Telegram
- Node 执行摘要回传给 Master
- 由 Master 统一写任务日志与发送 Telegram

## 6. 当前已补齐的 UX 细节

- Safari / 非安全上下文复制降级方案
- 节点注册成功弹窗重排
- 节点分配弹窗增加全选 / 清空
- 域名筛选器与部分 Select 改为显示用户可读标签，而不是内部值

## 7. 合并前建议校验

```bash
cd web/frontend
npm run lint
npm run build

cd ../..
python3 -m compileall web/backend/app
bash -n cert-master-sync.sh
bash -n cert-node-agent.sh
bash -n cert-node-pull.sh
bash -n install.sh
```

建议再手动看一遍：

- 域名页筛选器
- 首次安装向导 / 登录跳转
- 节点分配弹窗
- 设置页语言选择
- 节点注册后的安装命令弹窗

## 8. 剩余边界

- 暂无完整自动化端到端测试
- Node 命令执行依赖 timer 轮询，不是秒级即时推送
- 前端构建目前有 Vite chunk size 提示，但不影响运行
