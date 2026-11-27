# FastInsight 服务器部署

`.github/workflows/tag-ci.yml` 在 main、PR 和版本标签上验证并构建；只有推送 `v*` 标签会通过 FastDeploy 部署到 `fastrd`。普通提交的 commit message 包含 `v` 不会触发部署。手动运行验证 workflow 也不会直接部署。

普通运行参数直接写在该 workflow 的 `runtime-env`，使用字面 `KEY=value`，不要加 shell 引号。敏感值用该文件 `runtime-secrets` 中的同名仓库 Secrets，使用 `toJSON` 保留特殊字符。SSH 五项来自 FastR-D 组织的 `FASTRD_SSH_*` Secrets；请勿把凭证写入 YAML 或构建产物。

构建 job 上传同一次运行的 `release.tar.gz` 和 `manifest.json`。FastDeploy 和打包器均固定到完整提交 `9a2c51dcd63f47a0e6c8f6bb07bd22ea589abbd2`，服务器不拉源码或重新安装依赖。代码相对路径由 [FastDeploy 应用配置](https://github.com/FastR-D/FastDeploy/blob/9a2c51dcd63f47a0e6c8f6bb07bd22ea589abbd2/config/apps.json) 注入，业务数据使用其中记录的原持久化路径。

发布示例（版本号每次必须唯一）：

```bash
git tag v2026.11.11.1
git push origin v2026.11.11.1
```

生产 Environment 仅允许 `v*` 标签和 main 上的手动回滚。切换后自动检查就绪，失败恢复旧代码和运行配置；数据库不会随代码回滚。需要回滚时，在 main 手动执行 `.github/workflows/rollback.yml`，输入部署日志中的准确当前 release ID；若当前版本已变化则拒绝执行。

适配 main 提交不会重启当前业务。首个生产标签仍需验证登录、集成功能及数据兼容性。完整运行时、共享目录、旧服务接管和检查限制见 [FastDeploy README](https://github.com/FastR-D/FastDeploy#readme)。

飞书 bot 无 HTTP 就绪端点，发布只检查进程连续 10 秒存活，不发送测试消息；长连接和上游业务需另行验收。状态保留在 /home/dev/fastinsight。


## uv 与数据库分发配置

Python 运行环境固定为 `.python-version` 中的 3.12.12。CI 使用 uv 0.11.19 从 `uv.lock` 导出
`requirements.txt`，检查导出文件没有漂移，再以 `uv pip sync --require-hashes --target vendor` 安装。
测试针对该 vendor 目录运行，FastDeploy 仍打包 vendor，并由服务器 Python 3.12 直接运行，服务器无需联网安装依赖。
本地开发使用 `uv sync --locked` 与 `uv run --locked`。修改依赖后需重新导出 requirements.txt。

运行时不再使用 `FASTINSIGHT_ROSTER`。旧 FastDeploy 服务配置中即便残留该变量，执行端也会忽略它。
先部署支持 `GET /api/fastinsight/roster` 的 FastResearch，再迁移配置、配置成员的 `receiver_ref`，最后启动新版 FastInsight。
新执行端读取失败时会停止本次分发，不会回退旧 YAML。工作台保存的修改在下一篇论文处理时生效。
一次性导入命令和服务身份配置见 README；管理员会话仅用于导入，不能加入运行时部署 Secrets。
