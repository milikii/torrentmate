# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (zh-custom fork)

- 完整中文界面（简体 / 繁体），自动识别 Telegram 的 `zh-hans`、`zh-hant`；
  按钮、命令菜单、推送文案全部本地化，英文 / 俄文 / 乌克兰文保留。
- 一条消息内多条磁力链接的批量识别、按 info_hash 去重、统一入库与结果汇总。
- 基于种子名称的自动分类：FC2 / 有码 / 无码 / 国产 / 未分类，规则可在配置中
  增删改（正则），无需重启即可生效。
- 自动归类可分两段执行：先按磁力链接 `dn` 参数判断，缺少名称的任务在元数据
  到达后由监控协程移动目录并通知用户。
- 下载进度卡片（原地刷新）、完成卡片（大小 / 用时 / 平均速度 / 分享率），
  以及完成后的「详情 / 仅删种子 / 连文件一起删」内联按钮。
- `/status` 服务器状态与各下载目录剩余空间；`/speed` 全局限速；
  `/rules` 查看分类规则；`/classify` 测试名称分类。
- 添加前按 info_hash 查重并跳过；任务出错、长时间无进度各提醒一次。
- 通知队列持久化到 `config/queue.json`，机器人重启后继续推送。
- 新增 `transmission.pause_on_complete`，可在下载完成后自动暂停做种。
- Docker 相关：时区（`TZ=Asia/Shanghai`，基础镜像已自带 tzdata）、`.env.example`、
  本地构建 compose 配置；容器与 Transmission 使用同一 `PUID:PGID` 运行，避免挂载目录权限问题。
- Dockerfile 改为按 `Pipfile.lock` 的锁定版本直接 `pip install`，镜像内不再安装 pipenv，
  构建更快、依赖更可预期。
- 测试：磁力解析、分类规则、队列持久化、下载监控循环（共 46 个用例）。

### Fixed

- 修复 `/delete_<id>` 在删除通知里调用 `get_torrents(id)` 的错误参数。
- 修复 `bot.getFile` 在新版 python-telegram-bot 中的用法（改为 `get_file`）。

## [1.4.0] - 2026-07-15

See the [1.4.0 release and migration notes](docs/releases/v1.4.0.md).

### Added

- Required explicit administrator configuration through `bot.super_user`,
  `SUPER_USER`, or `--super-user`.
- Regression tests for authorization, configuration, tracker downloads,
  torrent metainfo uploads, browser escaping, history, and utility functions.
- Python 3.14 CI jobs for tests, formatting, linting, static typing, dependency
  auditing, and container builds.
- Container and Transmission health checks.
- Bounded and expiring search-result caching.
- Configurable maximum tracker torrent size through `bot.max_torrent_size`.
- Structured JSON Lines download-history records with best-effort support for
  reading the legacy format.

### Changed

- Upgraded the container runtime from Python 3.13 to Python 3.14.6.
- Upgraded `python-telegram-bot` from 21.10 to 22.8.
- Upgraded Beautiful Soup, Requests, lxml, PyYAML, Pillow/qrcode, pydash, and
  development tools to their current stable releases.
- Pinned the bundled Transmission deployment to
  `lscr.io/linuxserver/transmission:4.1.1-r1-ls345`.
- Moved blocking tracker and Transmission operations away from Telegram's
  asynchronous event loop.
- Replaced the unmanaged Transmission monitoring thread with an
  application-owned asynchronous task and clean shutdown handling.
- Reused a single Transmission RPC client instead of creating one per action.
- Moved bot-command synchronization into Telegram application startup.
- Changed invitation tokens to cryptographically secure, single-use tokens
  with a 15-minute lifetime.
- Changed downloaded local torrent handling to pass metainfo to Transmission
  instead of exposing temporary paths as server-side filenames.
- Consolidated container builds into one hardened `Dockerfile`.
- Changed Docker dependency installation to use the lockfile directly in the
  system environment without a runtime Pipenv dependency.

### Fixed

- Prevented the first Telegram user contacting a fresh deployment from
  automatically becoming administrator.
- Prevented tracker usernames and passwords from appearing in debug logs.
- Prevented environment and CLI secrets from being persisted into YAML.
- Restored TLS certificate verification when tracker proxies are enabled.
- Fixed unsafe and colliding temporary torrent filenames and ensured temporary
  torrent and QR files are removed.
- Fixed local `.torrent` uploads failing with Transmission's
  `unrecognized info` response.
- Added URL encoding and HTTP status checks to tracker requests.
- Escaped tracker-provided values before rendering Telegram HTML.
- Validated download-directory callback values against configured directories.
- Fixed process exit status and exception logging for startup/runtime failures.
- Fixed deprecated logging calls and a tracker error-log argument mix-up.

### Removed

- Removed the unrelated `cookiejar` package.
- Removed the dummy `bs4` package in favor of the direct `beautifulsoup4`
  dependency.
- Removed the unused `mechanize` dependency.
- Removed `Dockerfile.nas`; configuration must be mounted or provided through
  environment variables rather than embedded in an image.
- Removed default `test` Transmission credentials from example configuration.

### Security

- Deny access when no explicit administrator is configured.
- Keep TLS verification enabled by default when using a proxy.
- Redact secrets from logs and avoid persisting runtime secrets.
- Validate untrusted tracker output, callback values, downloads, and temporary
  file handling.

[Unreleased]: https://github.com/adskyiproger/transmission-telegram-bot/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/adskyiproger/transmission-telegram-bot/compare/v1.3.4...v1.4.0
