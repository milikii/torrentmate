# Transmission Telegram Bot · 汉化增强版

> 本仓库 fork 自 [adskyiproger/transmission-telegram-bot](https://github.com/adskyiproger/transmission-telegram-bot)（上游 v1.4.0），
> 在保留原有全部功能的基础上做了**完整汉化**和**面向个人 NAS 的增强**。
> 上游英文文档见 [docs/README.upstream.md](docs/README.upstream.md)。

简单说：在 Telegram 里把磁力链接丢给机器人，它按文件名自动分好目录、丢进 Transmission，然后一路把进度和完成情况推回给你。

> 不想先部署？打开 [docs/preview.html](docs/preview.html) 可以直接看到推送文案与按钮的实际效果（静态预览）。

---

## 一、这版改了什么

| 你的需求 | 本仓库的实现 |
| --- | --- |
| 汉化 | 界面、按钮、命令菜单、所有推送文案全中文；简体 + 繁体，自动识别 Telegram 的 `zh-hans` / `zh-hant`；英文、俄文、乌克兰文保留 |
| 多磁力统一入库 | 一条消息里的 N 条磁力链接一次识别、按 info_hash 去重、批量添加，逐条回报成功/重复/失败 |
| 按名称自动分目录 | `FC2` → FC2 目录，`SSIS-123` 这类有码番号 → 有码目录，`麻豆/MD-xxx/糖心` → 国产目录，`无码/流出/1pondo` → 无码目录，识别不了 → 未分类 |
| 推送下载情况 | 每个任务一张**进度卡片**（原地刷新，不刷屏），完成后推送完成卡片（大小/用时/平均速度/分享率），带「仅删种子 / 连文件一起删 / 详情」按钮 |
| 顺手加的 | `/status` 服务器状态与各目录剩余空间、`/speed` 全局限速、重复任务自动跳过、完成后自动暂停做种、通知队列落盘（重启不丢推送）、失败/长时间无进度提醒 |

## 二、功能一览

**下载管理**

- 一条消息发多条磁力链接，一次全部入库
- 发送 `.torrent` 文件、http(s) 种子地址同样支持
- 添加前按 info_hash 查重，已存在直接跳过并告知
- 任务列表、详情、暂停/继续、删除，均可在 Telegram 内完成
- 完成后一键「仅删种子」（保留文件做种/整理）或「连文件一起删」

**自动分类**

- 基于种子名称（磁力链接的 `dn` 参数）实时判断分类
- 没有 `dn` 参数的磁力链接，会在拿到元数据后自动移动到目标目录，并通知你
- 规则是配置里的正则，改完保存即生效，无需重启
- `/rules` 查看当前规则，`/classify 名称` 立刻测试某条名称会归到哪

**通知**

- 进度卡片：状态、进度条、已下载/总大小、上下行速度、连接数、分享率、剩余时间
- 完成卡片：大小、用时、平均速度、分享率 + 操作按钮
- 出错、长时间无进度（默认 30 分钟）各提醒一次

## 三、快速部署

### 1. 准备

- 一台装了 Docker 的机器（NAS / 小主机 / VPS 均可）
- 一个 Telegram 机器人 token：找 [@BotFather](https://t.me/botfather) 创建
- 你的 Telegram 数字 ID：给机器人发条消息，浏览器打开
  `https://api.telegram.org/bot<TOKEN>/getUpdates`，找 `message.from.id`

### 2. 起服务

```shell
git clone <你的仓库地址> transmission-telegram-bot
cd transmission-telegram-bot

mkdir -p downloads config
cp .env.example .env
vi .env          # 填 TOKEN、SUPER_USER、TRANSMISSION_USER/PASSWORD

# 关键一步：容器以 .env 里的 PUID:PGID 运行，宿主机目录属主要一致
sudo chown -R 1000:1000 downloads config

docker compose up -d --build
docker compose logs -f torrentino   # 看到「下载监控已启动」即正常
```

首次启动会自动从 `templates/torrentino.yaml` 生成 `config/torrentino.yaml`。
想改下载目录、分类规则、推送频率，改这个文件后 `docker compose restart torrentino`。

> 已有自己的 Transmission、只想跑机器人？把 `transmission` 服务删掉，用单容器即可：
>
> ```shell
> docker run -d --name torrentino --restart unless-stopped \
>   -u 1000:1000 -e TZ=Asia/Shanghai \
>   -e TOKEN=你的token -e SUPER_USER=你的ID \
>   -e TRANSMISSION_HOST=192.168.1.10 -e TRANSMISSION_PORT=9091 \
>   -e TRANSMISSION_USER=用户名 -e TRANSMISSION_PASSWORD=密码 \
>   -v $(pwd)/config:/usr/src/app/config \
>   transmission-telegram-bot:zh
> ```
>
> 机器人用长轮询，只需要出网，不用映射任何端口。

### 3. 验证

给机器人发 `/help`，再发一条磁力链接试试。Transmission 面板在 `http://<你的IP>:9091`。

## 四、使用说明

### 1. 一次发多条磁力链接

直接把多条链接粘进对话框（每行一条，或空格分隔，夹着文字也行）：

```
这几部一起下
magnet:?xt=urn:btih:xxxx&dn=FC2-PPV-1234567
magnet:?xt=urn:btih:yyyy&dn=SSIS-123
magnet:?xt=urn:btih:zzzz&dn=麻豆MD-0123
```

机器人会先回一张预览：

```
🔗 识别到 3 条磁力链接：
1. FC2-PPV-1234567 → FC2
2. SSIS-123 → 有码
3. 麻豆MD-0123 → 国产

请选择保存方式：点「🤖 自动分类」按名称分目录，或直接指定目录。
```

点「🤖 自动分类」后逐条入库，最后给一份汇总（成功几个、跳过几个重复、失败原因）。
一次最多处理 50 条，超过会截断。

> 想全部丢进同一个目录？直接点下面的目录按钮即可，分类规则不会生效。

### 2. 自动分类是怎么判断的

默认规则（按顺序匹配，先命中者生效）：

| 分类 | 目标目录 | 命中条件（举例） |
| --- | --- | --- |
| `fc2` | `/downloads/FC2` | `FC2-PPV-1234567`、`fc2_ppv_4567890`、任何带 `FC2` 的名称 |
| `国产` | `/downloads/国产` | `国产`、`麻豆`、`MD-0123`、`糖心`、`天美`、`果冻`、`SWAG`、`探花`… |
| `无码` | `/downloads/无码` | `无码`/`無修正`/`流出`/`uncensored`/`leak`、`1pondo`、`10musume`、`xxx-UC` |
| `有码` | `/downloads/有码` | 通用番号 `ABC-123`、`SSIS001`（已排除 `x264`、`1080p`、`HEVC`、年份等误判） |
| 兜底 | `/downloads/未分类` | 以上都不匹配 |

配置示例（`config/torrentino.yaml`）：

```yaml
directories:
  FC2: /downloads/FC2
  有码: /downloads/有码
  无码: /downloads/无码
  国产: /downloads/国产
  未分类: /downloads/未分类

auto_classify:
  enabled: true
  fallback: 未分类
  rules:
    - name: fc2
      dir: FC2
      patterns:
        - '\bFC2[\s._-]*(?:PPV)?[\s._-]*\d{3,8}\b'
        - '\bFC2\b'
    - name: 国产
      dir: 国产
      patterns:
        - '国产|國產|国内自拍'
        - '麻豆|天美|果冻|精东|糖心|探花'
```

- 想加自己的分类：在 `rules` 里加一条 `name/dir/patterns`，`dir` 必须在 `directories` 里存在
- 想停用某条：给它加 `enabled: false`
- 不确定规则对不对：`/classify 你的文件名`

**为什么会有「未分类」**：磁力链接没带 `dn` 名称，或者名称里没有任何可识别特征。
前者会在拿到种子信息后自动移动并通知你；后者请补规则，或手动选择目录。

### 3. 进度与完成推送

下载中每个任务一条卡片，原地刷新（默认 45 秒一次，进度变化超过 5% 也会刷新）：

```
🎬 FC2-PPV-1234567
📌 状态：下载中 ｜ 📁 /downloads/FC2
📊 ███████░░░ 68.4%
💾 3.12GB / 4.56GB
⚡ ↓ 6.31MB/s ｜ ↑ 210.5KB/s
👥 连接 24 ｜ 🌱 分享率 0.08
⏳ 剩余 03:12
```

完成后同一张卡片变成：

```
✅ 下载完成
🎬 FC2-PPV-1234567
📁 /downloads/FC2
💾 4.56GB
⏱ 用时 00:26:41 ｜ ⚡ 平均 2.91MB/s
🌱 分享率 0.00
[ℹ️ 详情] [🗑 仅删种子]
[🗑 连文件一起删]
```

### 4. 命令表

| 命令 | 作用 |
| --- | --- |
| `/torrents` | 任务列表（分页，带暂停/删除入口） |
| `/status` | 任务数、实时速度、本次/累计流量、各目录剩余空间 |
| `/history` | 下载历史 |
| `/last_search` | 上次搜索结果 |
| `/rules` | 查看自动分类规则 |
| `/classify <名称>` | 测试某条名称会归到哪个目录 |
| `/speed 5M 1M` | 全局限速（下载 5MB/s、上传 1MB/s）；`/speed off` 取消 |
| `/start_all` `/stop_all` | 全部开始 / 全部暂停 |
| `/adduser` | 生成一次性邀请链接（15 分钟有效，带二维码） |
| `/help` | 帮助 |

## 五、关键配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `transmission.host/port/user/password` | localhost:9091 | Transmission RPC 地址，容器里填服务名 `transmission` |
| `transmission.delete_data` | `true` | `/delete` 命令是否连文件一起删 |
| `transmission.pause_on_complete` | `false` | 完成后自动暂停做种 |
| `directories` | 见上 | 按钮名 → Transmission 路径，需提前建好目录并给权限 |
| `auto_classify.enabled` | `true` | 是否显示「自动分类」按钮、是否自动移动 |
| `auto_classify.rules` | 见上 | 分类规则，正则，顺序匹配 |
| `notify.progress` | `true` | 是否推送进度卡片 |
| `notify.check_interval` | `30` | 轮询 Transmission 的间隔（秒） |
| `notify.progress_interval` | `45` | 同一卡片最短刷新间隔（秒） |
| `notify.milestones` | `[]` | 额外提醒节点，如 `[25, 50, 75]`，会另发短消息 |
| `notify.stalled_minutes` | `30` | 多久没进度提醒一次，`0` 关闭 |
| `bot.queue_file` | `config/queue.json` | 通知队列落盘位置，重启后继续推送 |

## 六、建议的目录规划

```
downloads/
├── FC2/
├── 有码/
├── 无码/
├── 国产/
└── 未分类/
```

- Jellyfin / Plex 直接把 `downloads` 挂成媒体库
- 目录名想用英文（避免个别系统的编码问题）也可以，改 `directories` 的**值**即可，按钮名保持中文

## 七、常见问题

**Q：`docker compose up` 卡在拉取镜像 / 连不上 docker.io？**
国内网络常见。给 Docker 配个镜像加速器即可（改完重启 Docker）：

```shell
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<'JSON'
{ "registry-mirrors": ["https://docker.m.daocloud.io"] }
JSON
sudo systemctl restart docker
```

临时方案：先 `docker pull docker.m.daocloud.io/library/python:3.14.6-slim-bookworm`，
再 `docker tag` 成 `python:3.14.6-slim-bookworm`，然后 `docker compose up -d --build`。

Python 依赖下载慢的话，构建时换国内源：

```shell
docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose up -d
```

**Q：Transmission 报 “Permission denied” / 下载失败？**
宿主机 `downloads` 目录属主不是 `PUID:PGID`。执行 `sudo chown -R 1000:1000 downloads`，并确认 `.env` 里的 PUID/PGID 与之一致。

**Q：磁力链接添加成功，但卡片一直显示「正在获取种子元数据…」？**
说明没有连上任何 peer / tracker。检查网络、是否需要代理（`proxy` 段），或换个带 `tr=` 参数的链接。

**Q：分类不准，某个番号被归到「未分类」了？**
先用 `/classify 名称` 看命中了什么，再改规则。新番号一般能用通用规则识别；厂牌名特殊（如 `1pondo`）需要显式加进 `无码` 的 patterns。

**Q：会不会把普通电影误判成有码？**
通用规则已排除分辨率、编码、容器、年份等常见词，测试覆盖了 `The.Office.S01E01.1080p`、`Some.Movie.2024.2160p.HEVC` 这类名称。仍误判就把该条规则改成白名单式（列出常见厂牌前缀）。

**Q：机器人重启后，之前的下载还会推送吗？**
会。通知队列存在 `config/queue.json`，重启后继续刷新。

## 八、与上游同步

```shell
git remote add upstream https://github.com/adskyiproger/transmission-telegram-bot.git
git fetch upstream
git rebase upstream/main       # 有冲突时按提示解决
```

## 九、后续可以做什么

按投入产出排了优先级，见 [ROADMAP.md](ROADMAP.md)。最推荐先做的三件：关键词/RSS 订阅自动追更、完成后按番号自动重命名、磁盘水位保护。

## 授权

沿用上游 GPLv3 License，见 [LICENSE.md](LICENSE.md)。本项目为上游的衍生作品，保留原作者版权声明，修改部分同样以 GPLv3 分发。
