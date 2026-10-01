# 明日方舟自托管平台

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey.svg)](https://www.kernel.org/)
[![RAM](https://img.shields.io/badge/RAM-4GB%2B-brightgreen.svg)](#硬件与系统要求)

一个跑在 Linux 小主机上的明日方舟多账号自动托管系统。包含 Web 控制台、实时投屏、多账号隔离、定时任务调度，以及可选的微信通知。

---

## 演示

> 下面位置放演示 GIF 或截图。建议录制一段 30 秒左右的流程：切换账号 → 调整排班 → 任务启动 → 手机端查看战报。

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│              [ 在此插入 demo.gif 或截图 ]                │
│                                                          │
│   建议：docs/demo.gif，宽约 800px，README 中用           │
│   ![demo](docs/demo.gif) 引用即可                        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

如果你已经跑起来并愿意贡献素材，欢迎提 PR 把截图放进 `docs/` 目录。

---

## 目录

- [这是什么](#这是什么)
- [适合谁用](#适合谁用)
- [硬件与系统要求](#硬件与系统要求)
- [技术架构](#技术架构)
- [核心能力](#核心能力)
- [部署步骤](#部署步骤)
- [外网访问：免费域名 + Cloudflare Tunnel](#外网访问免费域名--cloudflare-tunnel)
- [常见问题 (FAQ)](#常见问题-faq)
- [未来规划](#未来规划)
- [贡献者](#贡献者)
- [许可证](#许可证)

---

## 这是什么

市面上的方舟托管方案通常分两类：一类是收费的云端托管，账号密码要交到别人手里；另一类是自己开着台式机跑模拟器，功耗高、常年不关机也不现实。

这个项目走第三条路：把整套托管跑在家里那台本来就 24 小时通电的小机器上。模拟器平时处于停止状态，只有到了你设定的时段才按需启动，跑完任务立刻释放内存。控制面板常驻内存约 20MB。

它不是某个现成工具的封装，而是把几个组件粘合起来的一套完整服务：

- 用 **ReDroid** 提供无头 Android 环境（直接跑在宿主内核上，比传统虚拟机轻得多）；
- 用 **MAA** 做图像识别和日常任务执行；
- 用 **ADB** 在两者之间传指令；
- 用**一个 Python 脚本**提供 Web 控制台、投屏和 API。

目标只有两个：省电，以及账号数据完全留在自己手里。

---

## 适合谁用

- 家里有一台常开的 Linux 小主机（软路由、迷你主机、NAS、工控机、旧笔记本都行）；
- 有多个方舟账号，或者需要帮亲友一起打理日常；
- 不想把游戏账密托管在第三方平台；
- 希望模拟器平时不占资源，只在需要时跑一下。

如果你只有一台主力电脑，平时就想随手点两下 MAA，那这个项目对你来说偏重了，直接用桌面版 MAA 更合适。

---

## 硬件与系统要求

平时（绝大多数时间）只有 Web 面板在跑，内存占用约 **20MB**，CPU 基本为 0。到定时任务时段才会拉起模拟器和 MAA，此时内存峰值约 **1.8–2.5GB**，CPU 占用约 15%–35%，任务结束后立刻回落。

最低配置：

| 项目 | 要求 |
| --- | --- |
| CPU | 2 核及以上，64 位（x86_64 或 ARM64） |
| 内存 | 4GB（多账号建议 8GB） |
| 存储 | 25GB 以上可用空间（安卓镜像 + 游戏本体） |
| 系统 | Linux，内核 ≥ 5.4，需支持 `binderfs` |
| 其他 | Docker 与 Docker Compose |

---

## 技术架构

```
浏览器 / 微信通知
      │
      ▼
Cloudflare Tunnel（可选，用于外网访问）
      │
      ▼
Web 控制台（web_touch.py，常驻 ~20MB）
      │
      ▼
任务调度（maa_runner.py / db.py / scheduler_guard.py）
      │  ADB
      ▼
ReDroid 无头 Android 11 容器
      │
      ▼
MAA（图像识别与任务执行）
```

各组件职责：

- **Docker / Compose** — 环境隔离，不污染宿主机。
- **ReDroid** — 容器化的 Android 11。利用 Linux 原生 `binder` 机制，省掉了传统虚拟机的开销。
- **ADB** — 宿主机与安卓容器之间的控制通道（点击、输入、截图）。
- **MAA** — 负责识别与执行：基建换班、关卡清智、公招、信用商店购物等。
- **scheduler_guard.py** — 生成并同步系统 crontab，处理多账号时段冲突。
- **web_touch.py** — 单个 Python 脚本，提供控制台、实时投屏、SQLite 持久化，无重型 Web 框架依赖。

---

## 核心能力

**账号隔离。** 每个方舟账号使用独立的 `shared_prefs` 存档，登录态互不干扰。这是多账号托管最容易出问题的地方，本项目做了专门处理。

**全局账号切换。** 投屏、排班、关卡、物资查询都跟随右上角的账号下拉框，切换后各面板同步刷新。

**公招限额。** 每天按设定的配额消耗招聘许可（默认 4 次），拉满 9 小时；配额用尽后只收干员刷词条，禁止使用加急券。避免一觉醒来许可全被清空。

**物资与芯片看板。** 同步合成玉、源石、龙门币、固源岩等资源，以及 14 种职业的初级/组/双芯片和芯片助剂。

**通知推送。** 任务结束后生成 Markdown 报告，支持 WxPusher（微信）与邮件。

**防时段冲突。** 新增账号时若与其他账号的执行时段间隔小于 60 分钟，会被拦截并提示。

---

## 部署步骤

### 1. 启用内核 binderfs 支持

ReDroid 依赖内核的 `binderfs`。先检查：

```bash
ls -l /dev/binderfs
```

若不存在，手动加载并挂载：

```bash
sudo modprobe binder_linux
sudo mkdir -p /dev/binderfs
sudo mount -t binder binder /dev/binderfs
```

需要开机自动执行的话，把上面三行写进 `/etc/rc.local`。

### 2. 克隆项目并生成配置

```bash
git clone https://github.com/hukaixukai/arknights-server.git
cd arknights-server

cp config/accounts.example.json config/accounts.json
cp config/system_settings.example.json config/system_settings.json
cp config/users.example.json config/users.json
```

然后按注释填写 `config/*.json`，至少要在 `users.json` 里设置管理员密码哈希，在 `accounts.json` 里填入账号信息。

### 3. 启动服务

```bash
# 安卓无头容器
docker compose up -d

# Web 控制台
docker compose -f compose-web.yml up -d
```

浏览器打开 `http://<小主机IP>:8090` 即可访问。

### 4. 配置定时任务

系统会根据各账号的排班时间自动写入 crontab，无需手动编辑。账号策略里设定好每天的执行时段即可。

---

## 外网访问：免费域名 + Cloudflare Tunnel

家用宽带一般没有公网 IPv4，也不建议在路由器上开端口映射。推荐组合是：**免费二级域名 + Cloudflare 托管解析 + Cloudflare Tunnel 穿透**，全程零成本。

### 获取免费域名

1. 打开 [DNSHE（https://my.dnshe.com/）](https://my.dnshe.com/)，用 GitHub 账号登录；
2. 在控制台领取一个免费二级域名（例如 `xxx.cc.cd`）；
3. 把域名的 NS 记录指向 Cloudflare，交给 Cloudflare 免费托管。

> 如果已有付费域名（阿里云 / 腾讯云 / NameSilo 等），同样可以把 NS 改到 Cloudflare。

### 用 Cloudflare Tunnel 穿透

1. 登录 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/)；
2. 进入 **Networks → Tunnels**，点击 **Create a Tunnel**，起个名字（如 `ark-tunnel`）；
3. 选择 Docker 部署，复制生成的命令，在小主机上后台运行：

   ```bash
   docker run -d --name cloudflare_tunnel --restart=always \
     cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <你的_TOKEN>
   ```

4. 在 **Public Hostname** 标签页添加一条路由：
   - **Subdomain**：`ark`（或任意前缀）
   - **Domain**：选择上一步托管进来的域名
   - **Type**：`HTTP`
   - **URL**：`localhost:8090`
5. 保存。之后手机访问 `https://ark.你的域名` 即可。

这样做的好处：不需要公网 IP，不需要端口映射，HTTPS 证书由 Cloudflare 自动签发，且个人免费额度完全够用。

---

## 常见问题 (FAQ)

### 部署相关

**Q：容器起来了，但 ADB 连不上模拟器（`adb devices` 为空）？**

先确认容器状态和端口：
```bash
docker ps | grep redroid
adb connect 127.0.0.1:5555
adb devices
```
常见原因有三个：
- 内核 `binderfs` 没挂载成功（见[部署步骤](#部署步骤)第 1 步，检查 `ls /dev/binderfs`）；
- `redroid` 容器启动后需要等待 30-60 秒系统才完成开机，用 `adb shell getprop sys.boot_completed` 确认返回 `1`；
- 宿主机的 adb 版本过旧，建议 1.0.41 以上。

**Q：`docker compose` 报错找不到 `adb` 或权限不足？**

`compose-web.yml` 里把宿主机的 adb 挂载进容器，路径写死为 `/usr/bin/adb`。如果你的 adb 装在别处（比如 `~/.local/bin/adb`），需要同步改两处：`ADB_BIN` 环境变量和 volumes 的映射路径。

**Q：需要 GPU 硬件加速吗？没有独显能跑吗？**

不是必需。`docker-compose.yml` 里默认映射了 `/dev/dri/*` 做 GPU 直通，如果你的小主机没有核显或设备路径不同，删掉 `devices` 那两行即可，ReDroid 会退回软件渲染，速度慢一些但能正常跑。

**Q：为什么任务跑完模拟器就关了？我想一直开着。**

这是刻意设计——平时不占资源。如果确实想常驻，把账号配置里的 `on_complete` 从 `stop_emu` 改成其他值（或不自动停止），但注意会持续占用约 1.8-2.5GB 内存。

### 账号相关

**Q：多个账号会不会串号？**

不会。每个账号使用独立的 `shared_prefs` 存档（存在 `data/account_profiles/`），登录态物理隔离。这是本项目专门处理的核心问题。首次添加账号时会自动完成登录态导出。

**Q：公招的加急券怎么保证不被乱用？**

代码里对公招任务固定设置了 `expedited: False`，且受 `daily_limit` 控制（默认 4 次/天）。配额用尽后只收干员、刷词条，不再消耗招聘许可。相关逻辑在 `bin/maa_runner.py`。

**Q：新增账号时提示"时段冲突"？**

系统要求不同账号的执行时段间隔至少 60 分钟，避免模拟器同时被多个任务抢占。调整排班时间错开即可。

### 网络相关

**Q：没有公网 IP，能在外面访问面板吗？**

可以，用 Cloudflare Tunnel，见[外网访问](#外网访问免费域名--cloudflare-tunnel)章节。全程免费，不需要端口映射。

**Q：Cloudflare Tunnel 和直接端口映射哪个好？**

强烈建议用 Tunnel。端口映射会把内网服务直接暴露在公网，容易被扫描；Tunnel 是主动向外建立连接，还自带 HTTPS。

### 其他

**Q：这个和直接用桌面版 MAA 有什么区别？**

桌面版 MAA 适合单账号、有人看着用。本项目的价值在多账号 + 无人值守 + 低功耗自托管。只有一台主力电脑、单账号的话，直接用桌面版 MAA 就行。

**Q：会封号吗？**

本项目只是自动化执行日常，不修改游戏数据、不使用外挂。但任何自动化都存在理论风险，请自行评估。建议合理安排任务频率，不要 24 小时不间断运行。

---

## 未来规划

当前架构把调度层和执行层分开了，MAA 只是执行层的一个实现。

后续计划探索**把 Mover 套进这个框架**，用 Mover 替代或配合 MAA 专门处理基建工作（跑单、无人机加速等），以提升基建收益。这目前只是方向，取决于 Mover 的接口成熟度，不一定落地。

---

## 贡献者

- **主要作者**：[hukaixukai](https://github.com/hukaixukai)
- **第二作者**：Gemini
- **第三作者**：DeepSeek

这是一个 **100% Vibe Coding** 项目。整套系统的设计、编码、调试和排障都在云端工作台中与 AI 结对完成——从无头 Android 容器适配、官方 SDK 登录态隔离，到公招限额算法和 Web 控制台交互，均是如此。Gemini 与 DeepSeek 分别在不同阶段参与了架构推演、代码生成与文档打磨。

---

## 许可证

[MIT License](LICENSE)。使用前请自行确认符合《明日方舟》最终用户协议。