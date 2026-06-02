# SSL Sync Frontend

`web/frontend` 是 SSL Sync Master 的 Web 管理控制台前端，基于 React + TypeScript + Vite。

## 技术栈

- React 19
- TypeScript
- Vite
- React Router
- TanStack Query
- Tailwind CSS
- shadcn/ui
- React Hook Form + Zod

## 当前前端能力

- 首次安装初始化向导
- Dashboard 总览与实时事件流
- 域名管理
  - 搜索
  - 状态筛选
  - DNS 渠道筛选
  - 批量申请 / 续签 / 同步 / 启停 / 删除
- DNS 渠道管理
  - Cloudflare 单 Token 模式
  - 多 Provider 动态表单
  - 自定义 Provider 键值对
- 节点管理
  - 注册节点
  - 自动生成 node 一键接入命令
  - 节点详情、分配域名、批量下发 / 批量删除证书
- 任务日志
  - 状态 / 类型筛选
  - 日志查看与复制
- 系统设置
  - WebDAV / Telegram / ACME
  - 管理员账号密码修改
  - 中英文切换
  - 配置下载备份 / 上传恢复

## 开发模式

### Mock 模式

```bash
cd web/frontend
npm install
VITE_USE_MOCKS=true npm run dev
```

Mock 模式会拦截 `/api` 请求，提供完整的前端联调能力，不依赖后端服务。

首次打开 Mock 模式时也会进入初始化向导；初始化状态会保存在浏览器 `localStorage` 中。

默认开发地址：

```text
http://127.0.0.1:5173
```

### 连接真实后端

```bash
cd web/frontend
npm install
VITE_API_BASE_URL=http://127.0.0.1:8080/api npm run dev
```

常用环境变量：

```env
VITE_API_BASE_URL=http://127.0.0.1:8080/api
VITE_USE_MOCKS=false
```

## 构建与校验

```bash
npm run lint
npm run build
```

## 多语言

当前支持：

- 简体中文
- English

语言设置保存在浏览器 `localStorage` 中，切换后立即生效。

## 说明

- 本项目使用了较多受控 `Select`、弹窗、批量操作交互，合并前建议至少手动过一遍：
  - 首次安装向导 / 登录跳转
  - 域名页筛选器
  - 节点分配弹窗
  - 节点注册成功弹窗
  - 设置页语言 / 备份恢复
