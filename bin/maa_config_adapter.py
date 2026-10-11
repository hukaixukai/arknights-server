# -*- coding: utf-8 -*-
"""
MAA 桌面版协议与配置适配器核心 (MaaConfigAdapter)
- 1:1 严格对齐 MAA 官方桌面版 (WPF / gui.json / gui.new.json) 配置结构与字段命名
- 提供官方真实 TaskQueue 规范与默认模板（去除自动肉鸽与生息演算，完整保留其余默认项）
- 提供声明式 TASK_SCHEMA 元数据注册表，驱动前端全参数自适应渲染
- 提供 Copilot 自动战斗全参数模型 (编队、助战、连战、理智药、追加干员)
- 提供 40 分钟时段独占冲突计算引擎（跨午夜环形时钟精准检测）
- 提供旧配置自动兼容与无损升级迁移函数 (migrate_legacy_config)
"""

import os
import json
import time
import copy
import pathlib
from typing import Dict, List, Tuple, Optional, Any

# 默认 40 分钟独占窗口
DEFAULT_WINDOW_MINUTES = 40

# 剿灭常驻回退关卡 (当期剿灭不可用时的性价比最高常驻图：龙门市区)
ANNIHILATION_FALLBACK_STAGE = "LungmenDowntown@Annihilation"

# 芯片库存保持预设 (对齐本机 MAA 库存保持芯片方案: 芯片保留5, 芯片组保留8)
CHIP_DEPOT_PRESET = [
    {"Stage": "PR-A-1", "DropId": "3231", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-A-1", "DropId": "3261", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-A-2", "DropId": "3232", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-A-2", "DropId": "3262", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-B-1", "DropId": "3241", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-B-1", "DropId": "3251", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-B-2", "DropId": "3242", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-B-2", "DropId": "3252", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-C-1", "DropId": "3211", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-C-1", "DropId": "3271", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-C-2", "DropId": "3212", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-C-2", "DropId": "3272", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-D-1", "DropId": "3221", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-D-1", "DropId": "3281", "DropCount": 5, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-D-2", "DropId": "3222", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0},
    {"Stage": "PR-D-2", "DropId": "3282", "DropCount": 8, "UseMedicine": False, "MedicineCount": 0, "UseStone": False, "StoneCount": 0}
]

# 掉落物 -> 关卡 自动映射表 (芯片/常见材料, 供库存保持自动填充关卡名)
DROP_STAGE_MAP = {
    "3211": "PR-C-1", "3212": "PR-C-2",
    "3221": "PR-D-1", "3222": "PR-D-2",
    "3231": "PR-A-1", "3232": "PR-A-2",
    "3241": "PR-B-1", "3242": "PR-B-2",
    "3251": "PR-B-1", "3252": "PR-B-2",
    "3261": "PR-A-1", "3262": "PR-A-2",
    "3271": "PR-C-1", "3272": "PR-C-2",
    "3281": "PR-D-1", "3282": "PR-D-2",
    "30011": "1-7", "30012": "1-7",
    "4001": "CE-6",
}

# 芯片本掉落物可选项 (供库存保持计划编辑; label 不含括号内容)
CHIP_DROP_OPTIONS = [
    {"value": "3211", "label": "先锋芯片"},
    {"value": "3212", "label": "先锋芯片组"},
    {"value": "3221", "label": "近卫芯片"},
    {"value": "3222", "label": "近卫芯片组"},
    {"value": "3231", "label": "重装芯片"},
    {"value": "3232", "label": "重装芯片组"},
    {"value": "3241", "label": "狙击芯片"},
    {"value": "3242", "label": "狙击芯片组"},
    {"value": "3251", "label": "术师芯片"},
    {"value": "3252", "label": "术师芯片组"},
    {"value": "3261", "label": "医疗芯片"},
    {"value": "3262", "label": "医疗芯片组"},
    {"value": "3271", "label": "辅助芯片"},
    {"value": "3272", "label": "辅助芯片组"},
    {"value": "3281", "label": "特种芯片"},
    {"value": "3282", "label": "特种芯片组"},
    {"value": "30012", "label": "固源岩"},
    {"value": "30011", "label": "源岩"},
    {"value": "30062", "label": "聚酸酯组"},
    {"value": "30061", "label": "聚酸酯"},
    {"value": "4001", "label": "龙门币"}
]

def get_default_task_queue(account_name: str = "") -> List[Dict[str, Any]]:
    """
    生成 MAA 官方桌面版 TaskQueue 默认队列 (对齐 gui.new.json v6.18.0)
    - 开始唤醒：账号切换仅填尾号（需设备已手动登录过该账号）
    - 自动公招：仅暴露 强制刷新 / 次数上限 / 自动确认6★，其余吃 MAA 默认
    - 基建换班：三模式 (常规/队列轮换/自定义)
    - 刷剿灭：特化理智作战 (周计划门控)，仅暴露 刷取时间 + 刷取关卡
    - 库存保持：芯片全职业缺口补齐
    - 剩余理智：仅保留关卡，其余全部关闭
    """
    return [
        {
            "$type": "StartUpTask",
            "Name": "开始唤醒",
            "IsEnable": True,
            "ClientType": "Official",
            "StartGame": True,
            "AccountName": ""
        },
        {
            "$type": "RecruitTask",
            "Name": "自动公招",
            "IsEnable": True,
            "MaxTimes": 4,
            "ForceRefresh": True,
            "Level6Choose": False
        },
        {
            "$type": "InfrastTask",
            "Name": "基建换班",
            "IsEnable": True,
            "Mode": "Default",
            "UsesOfDrones": "PureGold",
            "Filename": "",
            "DormThreshold": 50,
            "DormTrustEnabled": True,
            "DormFilterNotStationed": True,
            "ReceptionMessageBoard": True,
            "ReceptionClueExchange": True,
            "SendClue": True,
            "ContinueTraining": False,
            "OriginiumShardAutoReplenishment": False,
            "RoomList": [
                {"Room": "Mfg", "IsEnabled": True},
                {"Room": "Trade", "IsEnabled": True},
                {"Room": "Control", "IsEnabled": True},
                {"Room": "Power", "IsEnabled": True},
                {"Room": "Reception", "IsEnabled": True},
                {"Room": "Office", "IsEnabled": True},
                {"Room": "Dorm", "IsEnabled": True},
                {"Room": "Processing", "IsEnabled": True},
                {"Room": "Training", "IsEnabled": False},
                {"Room": "AssistantChange", "IsEnabled": False}
            ]
        },
        {
            "$type": "AnnihilationTask",
            "Name": "刷剿灭",
            "IsEnable": True,
            "Stage": "Annihilation",
            "WeeklySchedule": {
                "Sunday": False,
                "Monday": True,
                "Tuesday": False,
                "Wednesday": False,
                "Thursday": False,
                "Friday": False,
                "Saturday": False
            }
        },
        {
            "$type": "DepotMaintainTask",
            "Name": "库存保持",
            "IsEnable": True,
            "UpdateDepot": True,
            "SkipDuringActivity": True,
            "OnlyFirstInsufficientPlan": True,
            "UseAutoSeries": True,
            "UseExpiringMedicine": True,
            "UseMedicine": False,
            "MedicineCount": 0,
            "UseStone": False,
            "StoneCount": 0,
            "PlanList": copy.deepcopy(CHIP_DEPOT_PRESET)
        },
        {
            "$type": "FightTask",
            "Name": "剩余理智",
            "IsEnable": True,
            "SanityOnly": True,
            "StagePlan": ["1-7"],
            "Series": 0,
            "UseMedicine": False,
            "MedicineCount": 0,
            "UseStone": False,
            "StoneCount": 0,
            "UseExpiringMedicine": True,
            "UseWeeklySchedule": False,
            "WeeklySchedule": {
                "Sunday": True,
                "Monday": True,
                "Tuesday": True,
                "Wednesday": True,
                "Thursday": True,
                "Friday": True,
                "Saturday": True
            }
        },
        {
            "$type": "MallTask",
            "Name": "信用收支",
            "IsEnable": True,
            "Shopping": True,
            "FirstList": "赤金：招聘许可：龙门币",
            "BlackList": "碳;家具;加急许可",
            "ReserveMaxCredit": True,
            "OnlyBuyDiscount": False,
            "ShoppingIgnoreBlackListWhenFull": False,
            "CreditFight": False,
            "CreditFightFormation": 0,
            "CreditFightOnceADay": True,
            "VisitFriends": True,
            "VisitFriendsOnceADay": True
        },
        {
            "$type": "AwardTask",
            "Name": "领取奖励",
            "IsEnable": True,
            "Award": True,
            "Mail": True,
            "FreeGacha": True,
            "Orundum": True,
            "Mining": True,
            "SpecialAccess": True,
            "SignInEvent": False
        },
        {
            "$type": "UserDataUpdateTask",
            "Name": "更新数据",
            "IsEnable": True,
            "UpdateOperBox": True,
            "UpdateDepot": True,
            "TriggerInterval": "Daily",
            "IsTriggered": True
        }
    ]

# 可选添加的任务目录（供 "+ 添加任务" 按钮使用）
TASK_CATALOG = [
    {"type": "FightTask", "name": "理智作战", "icon": "⚔️", "desc": "常规/活动/资源关卡理智刷取"},
    {"type": "AnnihilationTask", "name": "刷剿灭", "icon": "🛡️", "desc": "每周剿灭作战清理 (特化理智作战 + 周计划)"},
    {"type": "RecruitTask", "name": "自动公招", "icon": "📋", "desc": "公开招募标签识别与锁定"},
    {"type": "InfrastTask", "name": "基建换班", "icon": "🏢", "desc": "常规/队列轮换/自定义排班"},
    {"type": "MallTask", "name": "信用收支", "icon": "🛒", "desc": "信用商店购物与好友拜访"},
    {"type": "AwardTask", "name": "领取奖励", "icon": "🎁", "desc": "每日任务、邮件、单抽与开采"},
    {"type": "DepotMaintainTask", "name": "库存保持", "icon": "📦", "desc": "芯片与材料缺口自动连战补齐"},
    {"type": "UserDataUpdateTask", "name": "更新数据", "icon": "📊", "desc": "全量识别仓库材料与干员列表"},
    {"type": "StartUpTask", "name": "开始唤醒", "icon": "⚡", "desc": "前置启动游戏客户端与账号登录"}
]

# 声明式 TASK_SCHEMA：定义每个任务类型包含的所有官方原生可配置字段及其控件类型
# hide=true 的字段不在界面暴露（由 runner 按官方默认值自动填充）
TASK_SCHEMA = {
    "StartUpTask": [
        {"key": "ClientType", "label": "客户端平台", "type": "select", "options": [
            {"value": "Official", "label": "官方服 (Hypergryph)"},
            {"value": "Bilibili", "label": "Bilibili服"}
        ], "default": "Official"},
        {"key": "StartGame", "label": "自动启动客户端", "type": "bool", "default": True},
        {"key": "AccountName", "label": "切换账号尾号", "type": "text", "default": "", "hint": "仅需填写尾号(如 4567)。仅支持设备已手动登录过一次的账号，留空则沿用当前环境"}
    ],
    "RecruitTask": [
        {"key": "ForceRefresh", "label": "无 4/5/6★ 时强制刷新", "type": "bool", "default": True},
        {"key": "MaxTimes", "label": "每次运行招募次数上限", "type": "number", "min": 0, "max": 10, "default": 4},
        {"key": "Level6Choose", "label": "自动确认 6★ (高级资深)", "type": "bool", "default": False, "hint": "关闭时将停手并发送告警通知等待人工确认；3/4/5★ 与时长(540分)均为官方自动最优默认"}
    ],
    "InfrastTask": [
        {"key": "Mode", "label": "换班工作模式", "type": "select", "options": [
            {"value": "Default", "label": "常规模式 - MAA 官方算法自动最优换班 (无需文件)"},
            {"value": "Rotation", "label": "队列轮换 - 使用游戏内预设队列"},
            {"value": "Custom", "label": "自定义基建配置 - 导入排班作业文件"}
        ], "default": "Default"},
        {"key": "Filename", "label": "自定义排班文件 (仅自定义模式)", "type": "infrast_file", "default": "", "showWhen": {"Mode": "Custom"}},
        {"key": "UsesOfDrones", "label": "无人机加速目标", "type": "select", "options": [
            {"value": "PureGold", "label": "赤金制造"},
            {"value": "Money", "label": "龙门币贸易"},
            {"value": "SyntheticJade", "label": "合成玉制造"},
            {"value": "CombatRecord", "label": "作战记录制造"},
            {"value": "OriginStone", "label": "源石碎片制造"},
            {"value": "Chip", "label": "芯片制造"},
            {"value": "_NotUse", "label": "不使用无人机"}
        ], "default": "PureGold", "hideWhen": {"Mode": "Custom"}, "hint": "自定义模式下无人机用途由排班文件决定"},
        {"key": "DormThreshold", "label": "宿舍恢复心情阈值 (%)", "type": "number", "min": 10, "max": 100, "default": 50},
        {"key": "DormTrustEnabled", "label": "宿舍空位填入未满信赖干员", "type": "bool", "default": True},
        {"key": "DormFilterNotStationed", "label": "不将已进驻干员放入宿舍", "type": "bool", "default": True},
        {"key": "ReceptionMessageBoard", "label": "领取会客室留言板信用", "type": "bool", "default": True},
        {"key": "ReceptionClueExchange", "label": "开启线索交流", "type": "bool", "default": True},
        {"key": "SendClue", "label": "赠送好友线索", "type": "bool", "default": True},
        {"key": "ContinueTraining", "label": "训练室自动接续训练", "type": "bool", "default": False},
        {"key": "OriginiumShardAutoReplenishment", "label": "贸易站源石碎片自动补货", "type": "bool", "default": False}
    ],
    "AnnihilationTask": [
        {"key": "WeeklySchedule", "label": "刷取时间 (勾选执行的星期)", "type": "weekly", "default": {
            "Monday": True, "Tuesday": False, "Wednesday": False, "Thursday": False, "Friday": False, "Saturday": False, "Sunday": False
        }},
        {"key": "Stage", "label": "刷取关卡", "type": "select", "options": [
            {"value": "Annihilation", "label": "当期剿灭 (全权委托/PRTS代理卡，不可用时自动回退龙门市区)"},
            {"value": "Chernobog@Annihilation", "label": "切尔诺伯格 (常驻)"},
            {"value": "LungmenOutskirts@Annihilation", "label": "龙门外环 (常驻)"},
            {"value": "LungmenDowntown@Annihilation", "label": "龙门市区 (常驻·性价比最高，回退首选)"}
        ], "default": "Annihilation"}
    ],
    "FightTask": [
        {"key": "StagePlan", "label": "刷取关卡", "type": "stage_plan", "default": ["1-7"], "hint": "从下方版本/章节列表选择，可多选按顺序刷取"},
        {"key": "Series", "label": "连战倍率 (series)", "type": "select", "options": [
            {"value": -1, "label": "-1 - 禁用切换 (单次刷取)"},
            {"value": 0, "label": "0 - 自动选用最大可用倍率 (推荐)"},
            {"value": 1, "label": "1 次连战"},
            {"value": 2, "label": "2 次连战"},
            {"value": 3, "label": "3 次连战"},
            {"value": 4, "label": "4 次连战"},
            {"value": 5, "label": "5 次连战"},
            {"value": 6, "label": "6 次连战"}
        ], "default": 0},
        {"key": "UseExpiringMedicine", "label": "优先消耗临期理智药 (≤48h)", "type": "bool", "default": True},
        {"key": "UseMedicine", "label": "允许吃常规理智药", "type": "bool", "default": False},
        {"key": "MedicineCount", "label": "最大理智药数量", "type": "number", "min": 0, "max": 999, "default": 0},
        {"key": "UseStone", "label": "允许吃至纯源石碎石", "type": "bool", "default": False},
        {"key": "StoneCount", "label": "最大吃石头数量", "type": "number", "min": 0, "max": 99, "default": 0},
        {"key": "UseWeeklySchedule", "label": "启用周计划 (限定周几执行)", "type": "bool", "default": False},
        {"key": "WeeklySchedule", "label": "周计划安排 (周一至周日)", "type": "weekly", "default": {
            "Monday": True, "Tuesday": True, "Wednesday": True, "Thursday": True, "Friday": True, "Saturday": True, "Sunday": True
        }, "showWhen": {"UseWeeklySchedule": True}}
    ],
    "DepotMaintainTask": [
        {"key": "UpdateDepot", "label": "执行前先扫描仓库", "type": "bool", "default": True},
        {"key": "SkipDuringActivity", "label": "活动期间跳过库存保持", "type": "bool", "default": True},
        {"key": "OnlyFirstInsufficientPlan", "label": "仅补足首个缺口方案", "type": "bool", "default": True},
        {"key": "UseAutoSeries", "label": "自动选择连战倍率", "type": "bool", "default": True},
        {"key": "UseExpiringMedicine", "label": "消耗临期理智药", "type": "bool", "default": True},
        {"key": "UseMedicine", "label": "允许吃常规理智药", "type": "bool", "default": False},
        {"key": "UseStone", "label": "允许碎石", "type": "bool", "default": False},
        {"key": "PlanList", "label": "库存保持计划 (芯片/材料缺口)", "type": "plan_list", "default": [], "dropOptions": "CHIP"}
    ],
    "MallTask": [
        {"key": "Shopping", "label": "信用商店自动购物", "type": "bool", "default": True},
        {"key": "FirstList", "label": "优先购买列表 (冒号隔开)", "type": "text", "default": "赤金：招聘许可：龙门币"},
        {"key": "BlackList", "label": "黑名单列表 (分号隔开)", "type": "text", "default": "碳;家具;加急许可"},
        {"key": "ReserveMaxCredit", "label": "保留 300 信用防溢出", "type": "bool", "default": True},
        {"key": "OnlyBuyDiscount", "label": "仅购买折扣商品", "type": "bool", "default": False},
        {"key": "ShoppingIgnoreBlackListWhenFull", "label": "信用溢出时无视黑名单买空", "type": "bool", "default": False},
        {"key": "VisitFriends", "label": "访问好友基建获取信用", "type": "bool", "default": True},
        {"key": "VisitFriendsOnceADay", "label": "每日仅拜访一次", "type": "bool", "default": True},
        {"key": "CreditFight", "label": "借助战打 OF-1 刷明日信用", "type": "bool", "default": False},
        {"key": "CreditFightFormation", "label": "OF-1 所用编队序号 (0-4)", "type": "number", "min": 0, "max": 4, "default": 0}
    ],
    "AwardTask": [
        {"key": "Award", "label": "领取每日/每周任务奖励", "type": "bool", "default": True},
        {"key": "Mail", "label": "领取全量邮件奖励", "type": "bool", "default": True},
        {"key": "FreeGacha", "label": "领取限定池每日免费单抽", "type": "bool", "default": True},
        {"key": "Orundum", "label": "领取幸运墙/合成玉奖励", "type": "bool", "default": True},
        {"key": "Mining", "label": "领取采矿许可合成玉", "type": "bool", "default": True},
        {"key": "SpecialAccess", "label": "领取特别访问/月卡奖励", "type": "bool", "default": True},
        {"key": "SignInEvent", "label": "领取活动签到奖励", "type": "bool", "default": False}
    ],
    "UserDataUpdateTask": [
        {"key": "UpdateDepot", "label": "全量扫描仓库材料储备", "type": "bool", "default": True},
        {"key": "UpdateOperBox", "label": "全量扫描干员列表 (Box)", "type": "bool", "default": True},
        {"key": "TriggerInterval", "label": "触发周期", "type": "select", "options": [
            {"value": "Daily", "label": "每天执行一次"},
            {"value": "EveryTime", "label": "每次运行均执行"}
        ], "default": "Daily"}
    ]
}

def get_default_copilot_config() -> Dict[str, Any]:
    """
    生成 Copilot 自动战斗默认模型 (1:1 对齐 MAA WPF CopilotViewModel / Copilot.cs)
    注意：自动战斗参数运行时从界面读取、不落库；此结构仅用于给出界面初始状态。
    """
    return {
        "Form": True,                  # 自动编队 (AutoSquad)
        "UseCopilotList": False,       # 多作业模式
        "UseFormation": False,         # 使用指定编队
        "SelectFormation": 1,          # 编队栏位 1-4
        "SupportUnitUsage": 1,         # 助战: 1=WhenNeeded, 3=Random
        "UseSupportUnitUsage": False,
        "AddTrust": False,             # 追加信赖干员
        "AddUserAdditional": False,    # 追加自定干员
        "IgnoreRequirements": False,   # 忽视干员练度要求
        "UserAdditional": [],          # [{"Name": "...", "Skill": 1, "Module": 0}]
        "LoopTimes": 1,                # 循环次数 (单作业)
        "UseSanityPotion": False,      # 理智不足吃药 (多作业)
        "TaskList": []                 # 多作业列表 [{"Name","FilePath","IsRaid","IsChecked"}]
    }

def get_default_maa_config(account_name: str = "") -> Dict[str, Any]:
    """生成完整的 MAA 配置模板 (真实官方结构)"""
    return {
        "Version": "v6.18.0",
        "ConfigName": "Default",
        "TaskQueue": get_default_task_queue(account_name),
        "Copilot": get_default_copilot_config(),
        "Toolbox": {
            "OperBoxExportFormat": "Clipboard",
            "GachaShowDisclaimerNoMore": False,
            "ChooseLevel3": False,
            "ChooseLevel3Time": 540,
            "ChooseLevel4": True,
            "ChooseLevel4Time": 540,
            "ChooseLevel5": True,
            "ChooseLevel6": True,
            "AutoSetTime": True
        }
    }

def get_default_maa_timers() -> Dict[str, Any]:
    """
    生成 MAA 原生定时执行模板 (Timers)
    默认预设 10:00 / 16:00 / 22:00 三个班次 (40 分钟独占)
    """
    return {
        "ForceScheduledStart": True,
        "ShowWindowBeforeForceScheduledStart": False,
        "CustomConfig": True,
        "WindowMinutes": DEFAULT_WINDOW_MINUTES,
        "List": [
            {"Id": 0, "IsEnabled": True, "Config": "Default", "Hour": 10, "Minute": 0, "Remark": "早班"},
            {"Id": 1, "IsEnabled": True, "Config": "Default", "Hour": 16, "Minute": 0, "Remark": "午班"},
            {"Id": 2, "IsEnabled": True, "Config": "Default", "Hour": 22, "Minute": 0, "Remark": "晚班"}
        ]
    }

# ==================== 理智作战关卡目录 (1:1 对齐 MAA 桌面版 StageManager) ====================
# MAA 不会列出全部 616 个关卡，而是「精选常驻关卡 + 在线活动关卡」两部分组成，
# 且在线部分随官方 API (gui/StageActivityV2.json) 每日更新 —— 这正是"资源更新"的体现。
#   - 常驻关卡：主线 1-7 / R8-11 / 12-17-HARD + 资源本(CE/AP/CA/LS/SK) + 芯片本(PR) + 剿灭
#   - 活动关卡：从 MAA 官方 API 动态拉取 (sideStoryStage)，带开放/过期时间
# 参考: MaaAssistantArknights/src/MaaWpfGui/Services/StageManager.cs
MAA_STAGE_ACTIVITY_API = "https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json"

# 常驻关卡定义: (code, 大类, 子分组, 周开放日 0=Mon..6=Sun; 空=全周, None=活动型不适用)
_PERMANENT_STAGES = [
    # 主线
    ("1-7", "主题曲 (主线)", "第 1 章", None),
    ("R8-11", "主题曲 (主线)", "第 8 章", None),
    ("12-17-HARD", "主题曲 (主线)", "第 12 章 (磨难)", None),
    # 资源本
    ("CE-6", "资源收集 (常驻)", "龙门币", [1, 3, 5, 6]),
    ("AP-5", "资源收集 (常驻)", "采购凭证", [0, 3, 5, 6]),
    ("CA-5", "资源收集 (常驻)", "技巧概要", [1, 2, 4, 6]),
    ("LS-6", "资源收集 (常驻)", "作战记录", []),
    ("SK-5", "资源收集 (常驻)", "碳素", [0, 2, 4, 5]),
    # 剿灭
    ("Annihilation", "资源收集 (常驻)", "剿灭作战", None),
    # 芯片本
    ("PR-A-1", "芯片搜索 (常驻)", "重装/医疗", [0, 3, 4, 6]),
    ("PR-A-2", "芯片搜索 (常驻)", "重装/医疗", [0, 3, 4, 6]),
    ("PR-B-1", "芯片搜索 (常驻)", "狙击/术师", [0, 1, 4, 5]),
    ("PR-B-2", "芯片搜索 (常驻)", "狙击/术师", [0, 1, 4, 5]),
    ("PR-C-1", "芯片搜索 (常驻)", "先锋/辅助", [2, 3, 5, 6]),
    ("PR-C-2", "芯片搜索 (常驻)", "先锋/辅助", [2, 3, 5, 6]),
    ("PR-D-1", "芯片搜索 (常驻)", "近卫/特种", [1, 2, 5, 6]),
    ("PR-D-2", "芯片搜索 (常驻)", "近卫/特种", [1, 2, 5, 6]),
]

# 剿灭可选传值 (协议要求)
_ANNIHILATION_VALUES = [
    ("Annihilation", "当期剿灭 (不可用时回退龙门市区)"),
    ("Chernobog@Annihilation", "切尔诺伯格 (常驻)"),
    ("LungmenOutskirts@Annihilation", "龙门外环 (常驻)"),
    ("LungmenDowntown@Annihilation", "龙门市区 (常驻·性价比最高)"),
]

_STAGE_CATALOG_CACHE = {"data": None, "ts": 0}
_STAGE_ACTIVITY_CACHE = {"data": None, "ts": 0}
_STAGE_ACTIVITY_TTL = 6 * 3600  # 内存缓存 6 小时 (磁盘缓存由每日更新维护)

# 与 MAA 桌面端一致：API 原始 JSON 落盘到 maa/cache/gui/StageActivityV2.json，
# 由每日资源更新 (update_maa.sh) 刷新；前端读取本地缓存，避免每次联网。
def _maa_cache_path(api: str) -> str:
    base = os.getenv("ARK_BASE_DIR") or str(pathlib.Path(__file__).resolve().parent.parent)
    return os.path.join(base, "maa", "cache", api)


def _load_disk_activity() -> Optional[Dict[str, Any]]:
    """读取本地磁盘缓存 (MAA 风格 maa/cache/gui/StageActivityV2.json)。"""
    p = _maa_cache_path("gui/StageActivityV2.json")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[!] 读取本地活动关卡缓存失败: {e}")
    return None


def _save_disk_activity(payload: Dict[str, Any]) -> None:
    """落盘保存 (供 update_maa.sh 与运行时共享)。"""
    p = _maa_cache_path("gui/StageActivityV2.json")
    try:
        d = os.path.dirname(p)
        os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] 写入本地活动关卡缓存失败: {e}")


def fetch_stage_activity(client_type: str = "Official", timeout: int = 12, allow_network: bool = True) -> Dict[str, Any]:
    """
    获取活动关卡数据 (gui/StageActivityV2.json)，与 MAA 桌面端同源:
      1) 优先读本地磁盘缓存 maa/cache/gui/StageActivityV2.json (由每日资源更新写入)；
      2) 磁盘/内存均无缓存且允许联网时，回源官方 API 并落盘。
    返回 { activities: [...], resourceCollection: {...} }
    """
    now = time.time()
    cache = _STAGE_ACTIVITY_CACHE
    if cache["data"] is not None and (now - cache["ts"]) < _STAGE_ACTIVITY_TTL:
        return cache["data"]

    # 1) 读磁盘缓存 (MAA 风格)
    payload = _load_disk_activity()

    # 2) 无磁盘缓存时回源 (并落盘)
    if payload is None and allow_network:
        try:
            import urllib.request
            req = urllib.request.Request(MAA_STAGE_ACTIVITY_API, headers={"User-Agent": "Mozilla/5.0 (MAA-Web)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            _save_disk_activity(payload)
        except Exception as e:
            print(f"[!] 回源 MAA 活动关卡 API 失败(将使用内置常驻关卡): {e}")
            payload = None

    result = {"activities": [], "resourceCollection": None}
    if isinstance(payload, dict):
        client = payload.get(client_type) or payload.get("Official") or {}
        rc = client.get("resourceCollection")
        if isinstance(rc, dict):
            result["resourceCollection"] = {
                "tip": rc.get("Tip"),
                "utc_start": rc.get("UtcStartTime"),
                "utc_expire": rc.get("UtcExpireTime"),
            }
        side = client.get("sideStoryStage")
        if isinstance(side, dict):
            for group_key, group in side.items():
                if not isinstance(group, dict):
                    continue
                activity = group.get("Activity") if isinstance(group.get("Activity"), dict) else {}
                stages = group.get("Stages") or []
                items = []
                for st in stages:
                    if not isinstance(st, dict):
                        continue
                    code = str(st.get("Value") or st.get("value") or "").strip()
                    if not code:
                        continue
                    items.append({
                        "code": code,
                        "display": str(st.get("Display") or code),
                        "drop": st.get("Drop"),
                    })
                if items:
                    result["activities"].append({
                        "key": group_key,
                        "name": activity.get("StageName") or group_key,
                        "tip": activity.get("Tip"),
                        "utc_start": activity.get("UtcStartTime"),
                        "utc_expire": activity.get("UtcExpireTime"),
                        "stages": items,
                    })
        cache["data"] = result
        cache["ts"] = now
    return result


def refresh_stage_activity_cache(client_type: str = "Official", timeout: int = 20) -> bool:
    """
    强制回源刷新磁盘缓存 (供 update_maa.sh 每日资源更新调用)。
    返回是否成功。
    """
    try:
        import urllib.request
        req = urllib.request.Request(MAA_STAGE_ACTIVITY_API, headers={"User-Agent": "Mozilla/5.0 (MAA-Web)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        _save_disk_activity(payload)
        _STAGE_ACTIVITY_CACHE["data"] = None
        _STAGE_CATALOG_CACHE["data"] = None
        return True
    except Exception as e:
        print(f"[!] 刷新活动关卡缓存失败: {e}")
        return False


def get_stage_catalog(stage_json_path: str = None, client_type: str = "Official", include_activity: bool = True) -> Dict[str, Any]:
    """
    构建 MAA 桌面版风格的关卡选择目录 (常驻精选 + 在线活动)。
    结构: { categories: [ {name, groups: [ {name, stages: [{code, label, hint}]} ]} ] }
    不再罗列全部 616 关；活动部分随官方 API 更新。
    """
    now = time.time()
    cache = _STAGE_CATALOG_CACHE
    # 常驻部分稳定；活动部分并入后在 TTL 内复用
    if cache["data"] is not None and (now - cache["ts"]) < 300:
        return cache["data"]

    categories: Dict[str, Dict[str, Any]] = {}

    # 1) 常驻关卡
    for code, major, sub, _days in _PERMANENT_STAGES:
        cat = categories.setdefault(major, {"name": major, "groups": {}})
        grp = cat["groups"].setdefault(sub, {"name": sub, "stages": []})
        label = "剿灭作战 (当期/常驻)" if code == "Annihilation" else code
        grp["stages"].append({"code": code, "label": label, "hint": ""})

    # 2) 剿灭专属可选传值
    res_cat = categories.setdefault("资源收集 (常驻)", {"name": "资源收集 (常驻)", "groups": {}})
    annih = res_cat["groups"].setdefault("剿灭作战 (传值)", {"name": "剿灭作战 (传值)", "stages": []})
    for code, label in _ANNIHILATION_VALUES:
        annih["stages"].append({"code": code, "label": f"{code}", "hint": label})

    # 3) 在线活动关卡 (随资源更新)
    if include_activity:
        act = fetch_stage_activity(client_type=client_type)
        for a in act.get("activities", []):
            name = a.get("name") or a.get("key")
            tip = a.get("tip") or ""
            cat = categories.setdefault("活动/主题关卡", {"name": "活动/主题关卡", "groups": {}})
            grp = cat["groups"].setdefault(name, {"name": name, "stages": []})
            for it in a.get("stages", []):
                grp["stages"].append({"code": it["code"], "label": it["display"], "hint": str(it.get("drop") or "")})
            if tip and not any(s.get("hint") for s in grp["stages"]):
                grp["hint"] = tip

    ordered = []
    for major in ["主题曲 (主线)", "资源收集 (常驻)", "芯片搜索 (常驻)", "活动/主题关卡"]:
        if major in categories:
            cat = categories[major]
            cat["groups"] = list(cat["groups"].values())
            ordered.append(cat)

    result = {"categories": ordered}
    cache["data"] = result
    cache["ts"] = now
    return result



def migrate_legacy_config(config_dict: Dict[str, Any], account_name: str = "") -> Dict[str, Any]:
    """
    旧配置自动平滑迁移函数：
    若检测到老版本制造的非标字段（例如 DailyLimit, Stage, AnnihilationMonday 等），
    自动将其无损转换为真实的 MAA 官方字段并补全缺失默认值。
    """
    if not isinstance(config_dict, dict):
        return get_default_maa_config(account_name)

    cfg = copy.deepcopy(config_dict)
    cfg["Version"] = "v6.18.0"
    if "ConfigName" not in cfg:
        cfg["ConfigName"] = "Default"

    # 1. 迁移 TaskQueue
    raw_queue = cfg.get("TaskQueue", [])
    if not isinstance(raw_queue, list) or len(raw_queue) == 0:
        cfg["TaskQueue"] = get_default_task_queue(account_name)
    else:
        migrated_queue = []
        for item in raw_queue:
            if not isinstance(item, dict):
                continue
            t_type = item.get("$type")
            if not t_type:
                continue

            # 忽略历史的肉鸽与生息
            if t_type in ("RoguelikeTask", "ReclamationTask"):
                continue

            m_item = copy.deepcopy(item)

            # 开始唤醒：保留用户填写的切换账号尾号 (AccountName)。
            # 历史遗留的 AccountSwitchEnabled 开关已从界面移除，若仍以其为门控会
            # 导致用户保存的尾号每次都被静默清空 —— 这里直接放行，并清理废弃字段。
            # 触屏切换是否真正发起仍由 runner 依据「设备当前账号 == 目标」动态裁决。
            if t_type == "StartUpTask":
                m_item.setdefault("AccountName", "")
                m_item.pop("AccountSwitchEnabled", None)

            # 公招迁移 (精简为 3 字段)
            elif t_type == "RecruitTask":
                if "DailyLimit" in m_item and "MaxTimes" not in m_item:
                    m_item["MaxTimes"] = int(m_item.get("DailyLimit", 4))
                if "Select6StarStrategy" in m_item and "Level6Choose" not in m_item:
                    m_item["Level6Choose"] = (m_item.get("Select6StarStrategy") == "auto_confirm")
                m_item = {
                    "$type": "RecruitTask",
                    "Name": m_item.get("Name", "自动公招"),
                    "IsEnable": m_item.get("IsEnable", True),
                    "ForceRefresh": m_item.get("ForceRefresh", True),
                    "MaxTimes": int(m_item.get("MaxTimes", 4)),
                    "Level6Choose": bool(m_item.get("Level6Choose", False))
                }

            # 基建迁移 (三模式 + 移除 PlanSelect)
            elif t_type == "InfrastTask":
                mode = m_item.get("Mode")
                if isinstance(mode, str):
                    ml = mode.lower()
                    if ml in ("rotation", "20000"):
                        new_mode = "Rotation"
                    elif ml in ("custom", "10000"):
                        new_mode = "Custom"
                    else:
                        new_mode = "Default"
                elif isinstance(mode, int):
                    new_mode = {0: "Default", 10000: "Custom", 20000: "Rotation"}.get(mode, "Default")
                else:
                    new_mode = "Default"
                m_item["Mode"] = new_mode
                m_item.pop("PlanSelect", None)
                for _fk in ("FiammettaRecoveryEnabled", "FiammettaTarget1", "FiammettaTarget2", "FiammettaTarget3", "CustomFileType", "UsePinusSylvestris", "UsePerceptionInformation", "UseWorldlyPlight", "UseAbyssalHunter"):
                    m_item.pop(_fk, None)
                m_item.setdefault("Filename", "")
                m_item.setdefault("UsesOfDrones", "PureGold")
                m_item.setdefault("DormThreshold", 50)
                m_item.setdefault("DormTrustEnabled", True)
                m_item.setdefault("DormFilterNotStationed", True)
                m_item.setdefault("ReceptionMessageBoard", True)
                m_item.setdefault("ReceptionClueExchange", True)
                m_item.setdefault("SendClue", True)
                m_item.setdefault("ContinueTraining", False)
                m_item.setdefault("OriginiumShardAutoReplenishment", False)
                m_item.setdefault("RoomList", [
                    {"Room": "Mfg", "IsEnabled": True},
                    {"Room": "Trade", "IsEnabled": True},
                    {"Room": "Control", "IsEnabled": True},
                    {"Room": "Power", "IsEnabled": True},
                    {"Room": "Reception", "IsEnabled": True},
                    {"Room": "Office", "IsEnabled": True},
                    {"Room": "Dorm", "IsEnabled": True},
                    {"Room": "Processing", "IsEnabled": True},
                    {"Room": "Training", "IsEnabled": False},
                    {"Room": "AssistantChange", "IsEnabled": False}
                ])

            # 剿灭特化迁移
            elif t_type == "AnnihilationTask":
                m_item = {
                    "$type": "AnnihilationTask",
                    "Name": m_item.get("Name", "刷剿灭"),
                    "IsEnable": m_item.get("IsEnable", True),
                    "Stage": m_item.get("Stage", "Annihilation"),
                    "WeeklySchedule": m_item.get("WeeklySchedule", {
                        "Sunday": False, "Monday": True, "Tuesday": False, "Wednesday": False, "Thursday": False, "Friday": False, "Saturday": False
                    })
                }

            # 战斗迁移 (精简字段 + 旧"刷剿灭"自动升级为 AnnihilationTask)
            elif t_type == "FightTask":
                # 旧版"刷剿灭"是一个带周计划或 Annihilation StagePlan 的 FightTask
                is_legacy_annih = (
                    m_item.get("UseCustomAnnihilation") is True
                    or (m_item.get("UseWeeklySchedule") and str(m_item.get("AnnihilationStage", "")).strip())
                    or (isinstance(m_item.get("StagePlan"), list) and m_item.get("StagePlan") == ["Annihilation"])
                )
                if is_legacy_annih and m_item.get("Name") == "刷剿灭":
                    m_item = {
                        "$type": "AnnihilationTask",
                        "Name": "刷剿灭",
                        "IsEnable": m_item.get("IsEnable", True),
                        "Stage": m_item.get("AnnihilationStage", "Annihilation"),
                        "WeeklySchedule": m_item.get("WeeklySchedule", {
                            "Sunday": False, "Monday": True, "Tuesday": False, "Wednesday": False, "Thursday": False, "Friday": False, "Saturday": False
                        })
                    }
                else:
                    if "Stage" in m_item and "StagePlan" not in m_item:
                        stg = str(m_item.get("Stage", "1-7")).strip()
                        m_item["StagePlan"] = [stg] if stg else ["1-7"]
                    # 精简为仅保留必要字段
                    new_item = {
                        "$type": "FightTask",
                        "Name": m_item.get("Name", "剩余理智"),
                        "IsEnable": m_item.get("IsEnable", True),
                        "StagePlan": m_item.get("StagePlan", ["1-7"]),
                        "Series": int(m_item.get("Series", 0)),
                        "UseExpiringMedicine": bool(m_item.get("UseExpiringMedicine", True)),
                        "UseMedicine": bool(m_item.get("UseMedicine", False)),
                        "MedicineCount": int(m_item.get("MedicineCount", 0)),
                        "UseStone": bool(m_item.get("UseStone", False)),
                        "StoneCount": int(m_item.get("StoneCount", 0)),
                        "UseWeeklySchedule": bool(m_item.get("UseWeeklySchedule", False)),
                        "WeeklySchedule": m_item.get("WeeklySchedule", {
                            "Sunday": True, "Monday": True, "Tuesday": True, "Wednesday": True, "Thursday": True, "Friday": True, "Saturday": True
                        })
                    }
                    if m_item.get("SanityOnly") or m_item.get("Name") == "剩余理智":
                        new_item["SanityOnly"] = True
                    m_item = new_item

            # 库存保持迁移 (芯片预设)
            elif t_type == "DepotMaintainTask":
                m_item.setdefault("UpdateDepot", True)
                m_item.setdefault("SkipDuringActivity", True)
                m_item.setdefault("OnlyFirstInsufficientPlan", True)
                m_item.setdefault("UseAutoSeries", True)
                m_item.setdefault("UseExpiringMedicine", True)
                m_item.setdefault("UseMedicine", False)
                m_item.setdefault("MedicineCount", 0)
                m_item.setdefault("UseStone", False)
                m_item.setdefault("StoneCount", 0)
                if not m_item.get("PlanList"):
                    m_item["PlanList"] = copy.deepcopy(CHIP_DEPOT_PRESET)

            # 信用迁移
            elif t_type == "MallTask":
                if "BuyFirst" in m_item and "FirstList" not in m_item:
                    bf = m_item.get("BuyFirst", [])
                    m_item["FirstList"] = "：".join(bf) if isinstance(bf, list) else str(bf)
                if "Blacklist" in m_item and "BlackList" not in m_item:
                    bl = m_item.get("Blacklist", [])
                    m_item["BlackList"] = ";".join(bl) if isinstance(bl, list) else str(bl)
                m_item.setdefault("Shopping", True)
                m_item.setdefault("ReserveMaxCredit", True)
                m_item.setdefault("OnlyBuyDiscount", False)
                m_item.setdefault("ShoppingIgnoreBlackListWhenFull", False)
                m_item.setdefault("VisitFriends", True)
                m_item.setdefault("VisitFriendsOnceADay", True)
                m_item.setdefault("CreditFight", False)
                m_item.setdefault("CreditFightFormation", 0)

            # 奖励迁移
            elif t_type == "AwardTask":
                m_item.setdefault("Award", True)
                m_item.setdefault("Mail", True)
                m_item.setdefault("FreeGacha", True)
                m_item.setdefault("Orundum", True)
                m_item.setdefault("Mining", True)
                m_item.setdefault("SpecialAccess", True)
                m_item.setdefault("SignInEvent", False)

            # 数据更新迁移
            elif t_type == "UserDataUpdateTask":
                m_item.setdefault("UpdateOperBox", True)
                m_item.setdefault("UpdateDepot", True)
                m_item.setdefault("TriggerInterval", "Daily")

            migrated_queue.append(m_item)
        cfg["TaskQueue"] = migrated_queue

    # 2. 迁移 Copilot (始终归一为最新界面结构; 参数运行时使用不落库)
    cop = cfg.get("Copilot", {})
    if not isinstance(cop, dict):
        cfg["Copilot"] = get_default_copilot_config()
    else:
        support_map = {"None": 0, "WhenNeeded": 1, "Always": 2, "Random": 3}
        new_cop = get_default_copilot_config()
        new_cop["Form"] = bool(cop.get("Form", cop.get("FormationEnabled", True)))
        new_cop["SelectFormation"] = int(cop.get("SelectFormation") or cop.get("FormationIndex", 1))
        usage = cop.get("SupportUnitUsage")
        if usage is None:
            usage = cop.get("SupportUsage")
        if usage is None and cop.get("SupportMode") in support_map:
            usage = support_map[cop["SupportMode"]]
        new_cop["SupportUnitUsage"] = int(usage) if usage in (1, 3) else 1
        new_cop["LoopTimes"] = int(cop.get("LoopTimes", 1))
        new_cop["UseSanityPotion"] = bool(cop.get("UseSanityPotion", False))
        new_cop["AddTrust"] = bool(cop.get("AddTrust", False))
        new_cop["IgnoreRequirements"] = bool(cop.get("IgnoreRequirements", False))
        new_cop["UserAdditional"] = cop.get("UserAdditional", []) if isinstance(cop.get("UserAdditional"), list) else []
        new_cop["TaskList"] = cop.get("TaskList", []) if isinstance(cop.get("TaskList"), list) else []
        cfg["Copilot"] = new_cop

    return cfg

def check_time_slot_overlap(h1: int, m1: int, h2: int, m2: int, dur1: int = 40, dur2: int = 40) -> bool:
    """
    基于 24 小时环形离散分钟集合的高精度时段重叠碰撞检测
    完美跨越午夜 23:59 -> 00:00 边界，100% 杜绝时序边界 Bug
    """
    start1 = (int(h1) * 60 + int(m1)) % 1440
    slot1 = set((start1 + i) % 1440 for i in range(max(1, int(dur1))))

    start2 = (int(h2) * 60 + int(m2)) % 1440
    slot2 = set((start2 + i) % 1440 for i in range(max(1, int(dur2))))

    return bool(slot1.intersection(slot2))

def format_time_span(hour: int, minute: int, duration: int) -> str:
    """格式化起止时间跨度，例如 10:00 ~ 10:40"""
    start_str = f"{int(hour):02d}:{int(minute):02d}"
    end_total = (int(hour) * 60 + int(minute) + int(duration)) % 1440
    end_str = f"{end_total // 60:02d}:{end_total % 60:02d}"
    return f"{start_str} ~ {end_str}"

def check_timers_conflict(
    target_acc_id: str,
    new_timers: Dict[str, Any],
    all_existing_timers: List[Dict[str, Any]],
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    acc_names_map: Optional[Dict[str, str]] = None
) -> Tuple[bool, Optional[str]]:
    """
    校验指定账号的新定时配置是否与全系统其他账号发生时段冲突
    返回 (is_valid, error_message)
    """
    acc_names = acc_names_map or {}
    new_list = new_timers.get("List", []) if isinstance(new_timers, dict) else []

    # 1. 检查新配置内部自身是否有重叠
    active_new = [t for t in new_list if t.get("IsEnabled")]
    for i in range(len(active_new)):
        for j in range(i + 1, len(active_new)):
            t1, t2 = active_new[i], active_new[j]
            h1, m1 = int(t1.get("Hour", 0)), int(t1.get("Minute", 0))
            h2, m2 = int(t2.get("Hour", 0)), int(t2.get("Minute", 0))
            if check_time_slot_overlap(h1, m1, h2, m2, window_minutes, window_minutes):
                span1 = format_time_span(h1, m1, window_minutes)
                span2 = format_time_span(h2, m2, window_minutes)
                return False, f"配置内部时段重叠：{span1} 与 {span2} 发生冲突，同一账号两次任务至少间隔 {window_minutes} 分钟。"

    # 2. 检查与其他账号的已启用定时冲突
    for other_rec in all_existing_timers:
        o_acc_id = other_rec.get("account_id")
        if o_acc_id == target_acc_id:
            continue  # 跳过自身旧配置

        o_timers_data = other_rec.get("timers") or other_rec.get("timers_json")
        if isinstance(o_timers_data, str):
            try:
                o_timers_data = json.loads(o_timers_data)
            except Exception:
                o_timers_data = {}
        if not isinstance(o_timers_data, dict):
            continue

        o_dur = int(other_rec.get("window_minutes") or o_timers_data.get("WindowMinutes") or window_minutes)
        o_list = o_timers_data.get("List", [])
        o_active = [t for t in o_list if t.get("IsEnabled")]
        o_name = acc_names.get(o_acc_id, other_rec.get("account_name", o_acc_id))

        for n_t in active_new:
            n_h, n_m = int(n_t.get("Hour", 0)), int(n_t.get("Minute", 0))
            for o_t in o_active:
                o_h, o_m = int(o_t.get("Hour", 0)), int(o_t.get("Minute", 0))
                if check_time_slot_overlap(n_h, n_m, o_h, o_m, window_minutes, o_dur):
                    n_span = format_time_span(n_h, n_m, window_minutes)
                    o_span = format_time_span(o_h, o_m, o_dur)
                    return False, (
                        f"时段冲突禁止保存！您设置的班次 【{n_span}】 与账号 [{o_name}] "
                        f"已占用的独占时段 【{o_span}】 发生冲突！"
                        f"单模拟器环境同一时段仅允许运行一个账号，请错开至少 {window_minutes} 分钟。"
                    )

    return True, None


# ==================== 关卡名合法性校验 (服务端权威) ====================
import re as _re

_STAGE_PATTERNS = [
    _re.compile(r"^Current$"),
    _re.compile(r"^[0-9]{1,2}-[0-9]{1,2}(-NORMAL|-HARD)?$", _re.I),
    _re.compile(r"^(R|TR|EG|LT)[0-9]{1,2}-[0-9]{1,2}$", _re.I),
    _re.compile(r"^(CE|LS|CA|AP|SK)-[0-9]{1,2}$", _re.I),
    _re.compile(r"^PR-[A-D]-[0-9]$", _re.I),
    _re.compile(r"^[A-Za-z]{2,4}-[0-9]{1,2}$"),
    _re.compile(r"^SSReopen-[A-Za-z0-9]+$", _re.I),
]
_ANNIHILATION_STAGES = {
    "Annihilation", "Chernobog@Annihilation",
    "LungmenOutskirts@Annihilation", "LungmenDowntown@Annihilation",
}


def is_valid_stage_code(code: str) -> bool:
    """校验单个关卡名是否合法 (与 MAA 支持的关卡命名一致)。"""
    s = str(code or "").strip()
    if not s:
        return False
    if s in _ANNIHILATION_STAGES:
        return True
    return any(p.match(s) for p in _STAGE_PATTERNS)


def validate_stage_plan(config_dict: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    校验 TaskQueue 中所有启用中的理智作战(FightTask)的 StagePlan 合法性。
    返回 (is_valid, error_message)。
    """
    if not isinstance(config_dict, dict):
        return True, None
    for t in config_dict.get("TaskQueue", []):
        if not isinstance(t, dict) or t.get("$type") != "FightTask":
            continue
        if not t.get("IsEnable", True):
            continue
        plan = t.get("StagePlan", [])
        if isinstance(plan, str):
            plan = [x.strip() for x in _re.split(r"[,，\s]+", plan) if x.strip()]
        plan = [str(x).strip() for x in (plan or []) if str(x).strip()]
        name = t.get("Name") or "理智作战"
        if not plan:
            return False, f"任务【{name}】未填写任何关卡名"
        bad = [x for x in plan if not is_valid_stage_code(x)]
        if bad:
            return False, f"任务【{name}】包含非法关卡名：{', '.join(bad)}（支持 1-7 / CE-6 / PR-A-1 / Annihilation / 活动关卡代码）"
    return True, None
