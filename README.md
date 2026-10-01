# Arknights Self-Host Hub · 明日方舟多账号低功耗自托管系统

> **关键词**：明日方舟 / Arknights / 多账号托管 / 自动挂机 / 基建排班 / 公招 / MAA / MaaAssistantArknights / ReDroid / 无头安卓 / Docker / 自托管 / Self-Hosted / 低功耗小主机 / 软路由 / NAS / 内网穿透 / Cloudflare Tunnel / 微信通知

一个跑在 Linux 小主机上的明日方舟多账号自动托管系统。包含 Web 控制台、实时投屏、多账号隔离、定时任务调度，以及可选的微信通知。

---

## 目录

- [这是什么](#这是什么)
- [适合谁用](#适合谁用)
- [硬件与系统要求](#硬件与系统要求)
- [架构](#架构)
- [核心能力](#核心能力)
- [部署步骤](#部署步骤)
- [第一次使用](#第一次使用)
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

其中最有价值的一块是**基建排班**：你可以导入 MAA 格式的排班文件，自己决定每天换几次班、每次用哪些干员，系统到点自动登入执行。详见[核心能力](#核心能力)。

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

## 架构

整个系统像一条流水线，从上到下一共四层。理解这四层，就能理解它是怎么工作的。

```
  ┌─────────────────────────────────────────────────┐
  │  ① 你（浏览器 / 微信）                          │
  │     打开网页下指令、收通知                       │
  └───────────────────────┬─────────────────────────┘
                          │ 可选：家里没公网 IP 时，走 Cloudflare Tunnel
                          ▼
  ┌─────────────────────────────────────────────────┐
  │  ② Web 控制台  web_touch.py                     │
  │     面板 + 投屏 + API，常驻内存约 20MB           │
  └───────────────────────┬─────────────────────────┘
                          │ 到点自动唤醒
                          ▼
  ┌─────────────────────────────────────────────────┐
  │  ③ 调度大脑                                     │
  │     scheduler_guard.py  写 crontab、防时段撞车   │
  │     maa_runner.py       干活的流水线             │
  │     db.py               存账号和数据的 SQLite    │
  └───────────────────────┬─────────────────────────┘
                          │ 通过 ADB 发号施令
                          ▼
  ┌─────────────────────────────────────────────────┐
  │  ④ 执行环境                                     │
  │     ReDroid   容器里的无头 Android 11           │
  │     MAA       图像识别、点按钮、跑任务           │
  └─────────────────────────────────────────────────┘
```

用一句话概括：**你在网页上设定排班 → 系统写好定时任务 → 到点自动把安卓容器叫起来 → MAA 在容器里登号干活 → 干完存好数据、关掉容器 → 给你发条微信。**

各文件职责：

| 文件 | 作用 |
| --- | --- |
| `bin/web_touch.py` | 网页控制台、实时投屏、REST API |
| `bin/maa_runner.py` | 核心流水线：开会话、切号、装配 MAA 任务链 |
| `bin/scheduler_guard.py` | 生成系统 crontab、校验多账号时段冲突 |
| `bin/db.py` | SQLite 存取（账号、用户、公招计数、物资） |
| `bin/notifier.py` | 微信 / 邮件通知 |
| `bin/update_maa.sh` | 更新 MAA 特征库与核心库 |
| `bin/update_arknights.sh` | 更新游戏本体 APK |
| `bin/ark_service_ctl.sh` | 手动控制模拟器与流水线运行 |
| `docker-compose.yml` | 定义 ReDroid 安卓容器 |
| `compose-web.yml` | 定义 Web 控制台容器 |

---

## 核心能力

### 1. 基建排班（最核心的功能）

直接从 MAA 或一图流排班表生成器导入排班文件（`.json`），自己决定两件事：

- **每天登入换班几次** —— 文件里有几个班次，就是每天登录几次。
- **每次换班用哪些干员** —— 每个班次里自由指定各房间的干员组合。

排班文件结构如下，每个 `plan` 是一个班次：

```json
{
  "title": "我的排班",
  "plans": [
    {
      "name": "早",
      "period": [["10:00", "16:00"]],
      "rooms": {
        "trading": [{ "operators": ["干员A", "干员B", "干员C"], "product": "LMD" }],
        "manufacture": [{ "operators": ["干员D", "干员E", "干员F"], "product": "PureGold" }],
        "power": [{ "operators": ["干员G"] }],
        "dormitory": [{ "operators": ["干员H", "干员I"] }],
        "control": [{ "operators": ["干员J"] }]
      }
    },
    {
      "name": "晚",
      "period": [["16:00", "22:00"]],
      "rooms": { "...": "同上结构" }
    }
  ]
}
```

- `period`：该班次的生效时段。可以写多段，跨午夜的晚班写成 `[["22:00", "23:59"], ["00:00", "10:00"]]`。
- `rooms`：房间键为 `trading`（贸易）`manufacture`（制造）`power`（发电）`dormitory`（宿舍）`control`（中枢）`meeting`（会客）`hire`（人力）`processing`（加工）。

系统按 `period` 判断当前该跑哪个班次，并同步到系统 crontab，到点自动登入执行。仓库里 `config/default_plan.example.json` 是一份三班次示例，可直接改名使用。

> 也可以不写排班表：把基建模式选成 `daily_once`，每天固定一个时间点做一次原生一键轮换。

### 2. 理智作战机制

日常模式下，流水线跑完前面的任务后，会用剩余理智去刷关卡，规则如下：

- **保芯片库存**：系统会维持账号内芯片储备在一个基准数量，不会盲目耗尽。
- **剩余理智刷 1-7**：如果芯片够、且做完任务后还有剩余理智，默认去刷 1-7 关，把理智清空（`fallback_stage` 默认 `1-7`，可改）。

### 3. 作战模式与手动执行

作战有两种模式，在账号策略里切换：

- **日常模式**（`daily_depot_maintain`）：清完日常后按上面的规则刷 1-7。
- **活动模式**（`manual_stage`）：优先刷你指定的活动主关卡（例如 `CW-10`），剩余理智再刷兜底关卡。

**手动执行单个关卡**：面板上可以直接选一个关卡让系统立刻跑一遍，跑完模拟器保持运行，方便在左侧大屏核对结果。

### 4. 自动更新（MAA 与游戏本体）

两个脚本，可以设成每天定时跑：

- `bin/update_maa.sh` —— 拉取 MAA 特征库，并比对 GitHub 最新 Release，有新版就热替换核心库（旧版备份到 `maa/backup/`）。
- `bin/update_arknights.sh` —— 探测官方 CDN 的最新游戏版本号，与模拟器内已装版本比对，不一致就下载并用 `install -r` 无损覆盖安装，**保留登录凭据**，完成后推送通知。

两者都会在需要时自动拉起模拟器、结束后归位休眠。配置方法见[部署步骤](#5-配置自动更新可选推荐)。

### 5. 其他能力

- **账号隔离**：每个账号独立的 `shared_prefs` 存档，登录态互不污染。
- **全局统一切换**：投屏、排班、关卡、物资全部跟随右上角账号下拉框联动。
- **公招限额**：每天按配额消耗招聘许可、拉满 9 小时；配额用尽只收干员刷词条，**禁用加急券**。
- **物资与芯片看板**：同步合成玉、源石、龙门币、固源岩及 14 种职业芯片。
- **通知推送**：任务结束生成 Markdown 报告，支持 WxPusher（微信）与邮件。
- **防时段冲突**：账号时段间隔小于 60 分钟会被拦截，避免抢占模拟器。
- **外网访问（可选）**：家里没公网 IP 时，可用 Cloudflare Tunnel 免费穿透，详见[外网访问](#外网访问免费域名--cloudflare-tunnel)。

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

然后按注释填写 `config/*.json`。管理员的密码哈希可以这样生成：

```bash
python3 -c "import hashlib; print(hashlib.sha256('你的密码'.encode()).hexdigest())"
```

把输出填进 `users.json` 的 `password_hash`。`accounts.example.json` 里的 `infrast.plan_file` 指向 `config/default_plan.example.json`，这是一份三班次示例排班表，可直接改名使用或替换成自己导出的排班文件。

> 注意：`config/` 下的真实配置文件（`accounts.json`、`users.json` 等）已在 `.gitignore` 中排除，不会被提交。`users.example.json` 里预置了一个随机生成的管理员密码，请按上面的命令换成自己的。

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

### 5. 配置自动更新（可选，推荐）

两个脚本可以设成每日定时执行，让 MAA 和游戏本体始终跟上最新版本：

```cron
# 每天凌晨检查并更新 MAA 与游戏本体
0 3 * * * /path/to/arknights-server/bin/update_maa.sh >> /path/to/arknights-server/maa/update.log 2>&1
30 3 * * * /path/to/arknights-server/bin/update_arknights.sh >> /path/to/arknights-server/update_apk.log 2>&1
```

也可以手动执行：

```bash
./bin/update_maa.sh                # 更新 MAA 特征库与核心库
./bin/update_arknights.sh          # 检查并更新游戏本体
./bin/update_arknights.sh --force  # 强制重新下载安装包
```

**更新逻辑说明：**

| 脚本 | 更新对象 | 版本判断方式 |
| --- | --- | --- |
| `update_maa.sh` | MaaResource 特征库 + MaaCore 核心库 | 比对 GitHub Release 最新 tag 与本地 `maa/version.txt` |
| `update_arknights.sh` | 明日方舟游戏本体 APK | 解析官方 CDN 包名中的版本号，与 `dumpsys package` 读取的已装版本比对 |

两个脚本都不会影响已保存的登录态：游戏用 `install -r`（覆盖安装 + 保留数据），MAA 只替换核心库并备份旧版。

> 提示：如果服务器访问 GitHub 较慢，可以把 `update_maa.sh` 里的下载地址换成 GitHub 加速镜像（在 URL 前拼 `https://ghfast.top/`），脚本内已用注释标出可替换的位置。

---

## 第一次使用

服务起来后，按这个顺序走一遍，就能从零跑通一个账号：

**1. 登录面板**
浏览器打开 `http://<小主机IP>:8090`，用 `config/users.json` 里设置的管理员账号登录。

**2. 添加账号**
进入「托管账号管理」，填游戏手机号、密码、账号昵称，选择平台（官服 / B服）。
首次添加时会自动完成一次登录并把该账号的登录态导出成独立存档，后续不再需要重复输入密码。

**3. 准备排班文件**
把排班文件（自己导出的，或用 `config/default_plan.example.json` 改名）通过面板上传到「基建排班」。文件里几个班次，系统就会每天登入几次。

**4. 设定策略**
在「托管账号策略」里设定：

- 每天的执行时段（例如 `10:00` / `16:00` / `22:00`）——这些时段会决定系统几点自动登号。
- 基建排班文件。
- 作战模式（日常 / 活动）与关卡。
- 公招每日上限、通知渠道等。

保存后，系统会自动把这些时段写进系统 crontab。

**5. 试跑一次**
回到「实时交互投屏」，点一下手动执行「全套日常」，观察模拟器被拉起、账号登录、任务依次执行。跑完可以在「物资与运行看板」看到最新的物资与芯片数据。

**6. 之后就交给它**
确认试跑正常，就不用再管了。系统会在你设定的时段自动唤醒、执行、退出，并把每日简报推送到你的微信 / 邮箱。

> 想单刷某个关卡，可以直接在面板上选关卡立刻执行，跑完模拟器保持运行，方便在大屏核对结果。

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

**Q：基建每天换几次班、用哪些干员，能自己定吗？**

可以，这也是本项目最主要的功能。导入 MAA 格式的排班文件即可：文件里每个 `plan` 是一个班次，班次数量决定每天登入换班的次数；每个班次的 `rooms` 里自由指定各房间的干员。系统按 `period` 时间段自动选择对应班次并同步到 crontab，到点自动执行。不写排班表也可以，用 `daily_once` 模式每天固定一个时间点做一次原生轮换。

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