# 知境 Studio 私有 Sites

站点部署 Studio 的 Vite 构建产物和同源 API 代理。Mac 网关连接正在使用的 Studio，数据库、模型、任务和资料由同一个 Mac 实例管理。

## 构建与部署

1. 保持 Mac Studio 运行。网关默认转发到 `127.0.0.1:5193`，可通过 `VKS_STUDIO_PORT` 指定其他现有实例。
2. 创建所有者私有 Sites，在独立源码目录写入平台生成的 `.openai/hosting.json`。构建 Studio 后执行 `node deploy/sites/stage.mjs <Sites 源码目录>`。该命令只复制生产构建和 Worker 源码。通过 Sites 流程运行该目录的 `npm run build`、保存和部署。
3. 在 Mac 设置 `VKS_SITES_ORIGINS` 为实际 Sites HTTPS 来源，用 `node deploy/sites/mac-gateway.mjs` 启动网关。网关从标准输入或 `VKS_SITES_GATEWAY_TOKEN` 读取随机共享凭据，默认仅监听 `127.0.0.1:5294`。
4. 将命名 Cloudflare Tunnel 和 Access 服务策略指向网关，为 Sites 设置服务端变量 `VKS_MAC_GATEWAY`、`VKS_SITES_GATEWAY_TOKEN`、`CF_ACCESS_CLIENT_ID` 和 `CF_ACCESS_CLIENT_SECRET`。站点凭据需通过 Sites 机密管理设置。重新部署保存版本使配置生效。

## 本机预览与检查

网关可为本机预览添加 `http://127.0.0.1:5293` 到逗号分隔的来源列表。运行 `node deploy/sites/preview.mjs <Sites 源码目录>/client`，在标准输入提供同一网关凭据。预览使用相同 Worker 逻辑，只监听回环地址。

`node --test deploy/sites/worker.test.mjs` 检查身份校验、跨来源写保护、远程 Bootstrap 限制及 Cookie 与流式请求转发。

站点默认打开标准文档视图。原型标签通过站点会话读取并校验原始资料，在浏览器中使用独立的不透明来源运行。原型可以操作界面和本地嵌套页面，其网络请求受浏览器策略限制。原型运行所需的文件随 Studio 构建部署。原样手册在本机 Studio 打开。

打包会用本机 Python 生成带版本指纹的文档助手外壳并压缩。浏览文档时站点预热外壳，只有遇到 Mermaid 图表才加载图表引擎。站点在当前已授权的资料范围内预取一个原型，Mac 网关可以压缩传输，浏览器解压后核对原始摘要。支持条件请求的原型可在本浏览器私有缓存中复用。每次重新使用缓存前，Mac 的原型内容接口先校验当前权限与版本。浏览器只保存每个账号最近一次校验通过的原型，退出工作台时清除。
