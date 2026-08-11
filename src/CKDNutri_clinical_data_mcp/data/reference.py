"""a207-his-mcp 数据生成参照表。

纯常量模块：人体测量参照值、病因谱、食物库、分期相关实验室区间。
所有数值仅用于生成虚拟演示数据集，不构成临床参考区间。
单位口径与 contracts/pcp-schema.json 一致。
"""

from __future__ import annotations

# 年龄带定义：band -> (最小年龄, 最大年龄, 中文标签)
AGE_BANDS: dict[str, tuple[float, float, str]] = {
    "toddler": (1.2, 3.9, "幼儿"),
    "preschool": (4.0, 6.9, "学龄前"),
    "school": (7.0, 12.9, "学龄"),
    "adolescent": (13.0, 17.5, "青少年"),
}

# 身高中位数参照 cm，键为整数年龄，值为 (男, 女)
HEIGHT_REF_CM: dict[int, tuple[float, float]] = {
    1: (76.5, 75.0), 2: (88.5, 87.2), 3: (96.8, 95.6), 4: (103.4, 102.7),
    5: (110.0, 109.0), 6: (116.5, 115.5), 7: (122.5, 121.5), 8: (128.0, 127.0),
    9: (133.0, 132.5), 10: (138.5, 138.5), 11: (144.0, 145.5), 12: (151.0, 152.0),
    13: (159.0, 156.5), 14: (165.5, 159.5), 15: (169.5, 160.5), 16: (171.5, 161.0),
    17: (172.5, 161.5), 18: (173.0, 161.8),
}

# 体重中位数参照 kg，键为整数年龄，值为 (男, 女)
WEIGHT_REF_KG: dict[int, tuple[float, float]] = {
    1: (10.1, 9.5), 2: (12.6, 12.0), 3: (14.7, 14.1), 4: (16.6, 16.0),
    5: (18.8, 18.1), 6: (21.2, 20.4), 7: (24.0, 22.9), 8: (27.2, 25.8),
    9: (30.5, 29.0), 10: (34.4, 33.0), 11: (38.6, 37.6), 12: (43.5, 42.5),
    13: (48.5, 46.7), 14: (53.4, 49.9), 15: (57.0, 51.8), 16: (59.4, 52.9),
    17: (61.0, 53.5), 18: (62.0, 53.8),
}

# 分期对应身高发育落后幅度（相对参照中位数的下调比例区间）
# 上限对应身高 Z 约 -3，仅在 G5 长期透析组出现少数重度矮小，与真实分布一致
STAGE_HEIGHT_DEFICIT: dict[str, tuple[float, float]] = {
    "G2": (0.005, 0.028),
    "G3a": (0.012, 0.045),
    "G3b": (0.025, 0.068),
    "G4": (0.038, 0.090),
    "G5": (0.050, 0.135),
}

# 分期对应最新一次 eGFR 取值区间 mL/min/1.73m2
STAGE_EGFR_RANGE: dict[str, tuple[float, float]] = {
    "G2": (62.0, 88.0),
    "G3a": (45.5, 59.0),
    "G3b": (30.5, 44.0),
    "G4": (15.5, 29.0),
    "G5": (8.0, 14.5),
}
# 已进入透析的患儿残余肾功能更低
DIALYSIS_EGFR_RANGE: tuple[float, float] = (3.5, 9.0)

STAGE_TO_NUMERIC: dict[str, int] = {"G2": 2, "G3a": 3, "G3b": 3, "G4": 4, "G5": 5}
# 与 pcp-schema.json diagnosis.dialysis_mode 枚举对齐
DIALYSIS_TO_PCP: dict[str, str] = {
    "none": "none", "HD": "hemodialysis", "PD": "peritoneal",
}
DIALYSIS_LABEL_ZH: dict[str, str] = {
    "none": "非透析", "HD": "血液透析", "PD": "腹膜透析",
}

# 病因谱按年龄带分布，贴合儿童 CKD 真实构成（幼儿以 CAKUT/遗传为主，青少年以肾小球疾病为主）
PRIMARY_DISEASE_BY_BAND: dict[str, list[str]] = {
    "toddler": ["先天性肾发育不良(CAKUT)", "后尿道瓣膜(CAKUT)", "先天性肾病综合征(芬兰型)",
                "常染色体隐性遗传多囊肾", "肾单位肾痨(NPHP)"],
    "preschool": ["先天性肾发育不良(CAKUT)", "反流性肾病(CAKUT)", "激素耐药型肾病综合征(FSGS)",
                  "溶血尿毒综合征后遗症", "肾单位肾痨(NPHP)"],
    "school": ["激素耐药型肾病综合征(FSGS)", "反流性肾病(CAKUT)", "Alport 综合征",
               "紫癜性肾炎", "IgA 肾病", "先天性肾发育不良(CAKUT)"],
    "adolescent": ["IgA 肾病", "Alport 综合征", "狼疮性肾炎", "局灶节段性肾小球硬化(FSGS)",
                   "紫癜性肾炎", "常染色体显性遗传多囊肾"],
}
# 命中即判定为肾小球源性疾病：低白蛋白、大量蛋白尿更常见
GLOMERULAR_KEYS: tuple[str, ...] = ("肾病综合征", "FSGS", "IgA", "狼疮", "紫癜", "Alport")

# 过敏原及其对应需回避的食物
ALLERGEN_FOODS: dict[str, tuple[str, ...]] = {
    "牛奶蛋白": ("牛奶", "酸奶", "奶酪"),
    "鸡蛋": ("鸡蛋", "蒸蛋羹"),
    "花生": ("花生酱",),
    "虾蟹类": ("虾仁",),
    "芒果": ("芒果",),
    "大豆": ("北豆腐", "豆浆"),
}

# 食物库：name -> (类别, 钾水平, 基准份量 g)
# 钾水平仅供生成器构造"高钾/低钾混合"的饮食日记，不随数据集导出，
# 权威成分数据由 a207-nutrition-calc-mcp 负责。
FOODS: dict[str, tuple[str, str, float]] = {
    "米饭": ("staple", "low", 120.0),
    "小米粥": ("staple", "low", 200.0),
    "白面馒头": ("staple", "low", 80.0),
    "挂面": ("staple", "low", 110.0),
    "吐司面包": ("staple", "low", 60.0),
    "藕粉": ("staple", "low", 150.0),
    "红薯": ("staple", "high", 100.0),
    "瘦猪肉": ("protein", "medium", 60.0),
    "鸡胸肉": ("protein", "medium", 60.0),
    "鲈鱼": ("protein", "medium", 70.0),
    "虾仁": ("protein", "medium", 50.0),
    "鸡蛋": ("protein", "medium", 50.0),
    "蒸蛋羹": ("protein", "medium", 90.0),
    "北豆腐": ("protein", "medium", 80.0),
    "牛肉": ("protein", "high", 55.0),
    "冬瓜": ("vegetable", "low", 120.0),
    "黄瓜": ("vegetable", "low", 100.0),
    "大白菜": ("vegetable", "low", 120.0),
    "生菜": ("vegetable", "low", 80.0),
    "白萝卜": ("vegetable", "low", 110.0),
    "菠菜": ("vegetable", "high", 100.0),
    "土豆": ("vegetable", "high", 110.0),
    "番茄": ("vegetable", "high", 100.0),
    "南瓜": ("vegetable", "high", 110.0),
    "鲜香菇": ("vegetable", "high", 60.0),
    "苹果": ("fruit", "low", 120.0),
    "梨": ("fruit", "low", 130.0),
    "西瓜": ("fruit", "low", 150.0),
    "香蕉": ("fruit", "high", 100.0),
    "橙子": ("fruit", "high", 130.0),
    "猕猴桃": ("fruit", "high", 80.0),
    "哈密瓜": ("fruit", "high", 140.0),
    "牛奶": ("dairy", "medium", 200.0),
    "酸奶": ("dairy", "medium", 150.0),
    "花生酱": ("other", "high", 15.0),
    "芒果": ("fruit", "high", 120.0),
}

# 各类食物在不同餐次的候选池
MEAL_SLOTS: dict[str, dict[str, int]] = {
    "breakfast": {"staple": 1, "protein": 1, "dairy": 1},
    "lunch": {"staple": 1, "protein": 1, "vegetable": 1},
    "dinner": {"staple": 1, "protein": 1, "vegetable": 1},
    "snack": {"fruit": 1},
}
# 餐次级别的候选收窄，避免早餐出现鲈鱼/牛肉这类不符合家庭习惯的组合
MEAL_CATEGORY_POOL: dict[tuple[str, str], tuple[str, ...]] = {
    ("breakfast", "staple"): ("小米粥", "白面馒头", "吐司面包", "挂面", "藕粉"),
    ("breakfast", "protein"): ("鸡蛋", "蒸蛋羹", "北豆腐"),
}
MEAL_LABEL_ZH: dict[str, str] = {
    "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐",
}

# 份量随年龄带缩放
PORTION_SCALE: dict[str, float] = {
    "toddler": 0.45, "preschool": 0.65, "school": 0.85, "adolescent": 1.10,
}

# 能量上限 kcal/kg/d，键为 (年龄带, 性别)
ENERGY_KCAL_PER_KG: dict[tuple[str, str], tuple[float, float]] = {
    ("toddler", "M"): (85.0, 100.0), ("toddler", "F"): (82.0, 96.0),
    ("preschool", "M"): (72.0, 85.0), ("preschool", "F"): (70.0, 82.0),
    ("school", "M"): (58.0, 70.0), ("school", "F"): (55.0, 65.0),
    ("adolescent", "M"): (42.0, 52.0), ("adolescent", "F"): (36.0, 45.0),
}

# 蛋白质 DRI 基线 g/kg/d
PROTEIN_DRI_G_PER_KG: dict[str, float] = {
    "toddler": 1.05, "preschool": 0.95, "school": 0.95, "adolescent": 0.85,
}
# 分期对 DRI 的倍数（PRNT 2020：G2 可至 140% DRI，G4-G5 收敛到 100%-110%）
STAGE_PROTEIN_FACTOR: dict[str, float] = {
    "G2": 1.40, "G3a": 1.30, "G3b": 1.20, "G4": 1.08, "G5": 1.00,
}
# 透析额外补充 g/kg/d（HD 补透析丢失，PD 另补腹膜蛋白丢失）
DIALYSIS_PROTEIN_ADD: dict[str, tuple[float, float]] = {
    "none": (0.0, 0.0), "HD": (0.08, 0.12), "PD": (0.15, 0.30),
}

# 电解质上限系数（mg / kcal），None 表示该分期不设定量上限
POTASSIUM_MG_PER_KCAL: dict[str, float | None] = {
    "G2": None, "G3a": None, "G3b": 1.10, "G4": 1.00, "G5": 0.85,
}
POTASSIUM_DIALYSIS_OVERRIDE: dict[str, float] = {"HD": 0.80, "PD": 1.05}
PHOSPHORUS_MG_PER_KCAL: dict[str, float | None] = {
    "G2": None, "G3a": None, "G3b": 0.55, "G4": 0.50, "G5": 0.45,
}
PHOSPHORUS_DIALYSIS_OVERRIDE: dict[str, float] = {"HD": 0.45, "PD": 0.50}

# 钠适宜摄入量 mg/d（中国居民膳食营养素参考摄入量年龄段），CKD 上限在此基础上按分期下调
SODIUM_AI_MG_BY_BAND: dict[str, float] = {
    "toddler": 700.0, "preschool": 900.0, "school": 1300.0, "adolescent": 1600.0,
}
SODIUM_RESTRICTION_FACTOR: dict[str, float] = {
    "G2": 1.00, "G3a": 1.00, "G3b": 0.90, "G4": 0.85, "G5": 0.80,
}

# 实验室指标基线区间（非透析、按分期）
LAB_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "G2": {"potassium": (3.8, 4.6), "phosphorus": (1.25, 1.75), "calcium": (2.25, 2.55),
           "albumin": (36.0, 46.0), "hemoglobin": (116.0, 132.0), "pth": (22.0, 60.0),
           "bun": (3.8, 7.0), "bicarbonate": (23.0, 26.5), "uric_acid": (240.0, 340.0)},
    "G3a": {"potassium": (4.0, 4.9), "phosphorus": (1.30, 1.85), "calcium": (2.20, 2.50),
            "albumin": (34.0, 44.0), "hemoglobin": (108.0, 125.0), "pth": (40.0, 95.0),
            "bun": (5.5, 9.5), "bicarbonate": (21.5, 25.0), "uric_acid": (280.0, 400.0)},
    "G3b": {"potassium": (4.2, 5.2), "phosphorus": (1.40, 1.95), "calcium": (2.15, 2.45),
            "albumin": (32.0, 42.0), "hemoglobin": (100.0, 118.0), "pth": (65.0, 160.0),
            "bun": (8.0, 13.5), "bicarbonate": (20.0, 24.0), "uric_acid": (320.0, 460.0)},
    "G4": {"potassium": (4.4, 5.6), "phosphorus": (1.55, 2.15), "calcium": (2.05, 2.40),
           "albumin": (30.0, 40.0), "hemoglobin": (90.0, 110.0), "pth": (110.0, 320.0),
           "bun": (12.0, 20.0), "bicarbonate": (18.0, 22.5), "uric_acid": (360.0, 520.0)},
    "G5": {"potassium": (4.6, 6.0), "phosphorus": (1.75, 2.45), "calcium": (1.95, 2.35),
           "albumin": (29.0, 39.0), "hemoglobin": (80.0, 104.0), "pth": (210.0, 620.0),
           "bun": (18.0, 30.0), "bicarbonate": (16.5, 21.0), "uric_acid": (390.0, 580.0)},
}

# 透析方式对基线区间的位移（加到抽样值上）
DIALYSIS_LAB_SHIFT: dict[str, dict[str, float]] = {
    "HD": {"potassium": 0.45, "phosphorus": 0.15, "albumin": 1.0, "hemoglobin": 8.0,
           "pth": 90.0, "bun": 2.5, "bicarbonate": 0.8},
    "PD": {"potassium": -0.55, "phosphorus": -0.10, "albumin": -3.5, "hemoglobin": 6.0,
           "pth": 40.0, "bun": -1.5, "bicarbonate": 1.5},
}

# 单位口径，与 contracts/pcp-schema.json units 一致
UNITS: dict[str, str] = {
    "scr": "umol/L", "egfr": "mL/min/1.73m2", "potassium": "mmol/L",
    "phosphorus": "mmol/L", "calcium": "mmol/L", "sodium": "mmol/L",
    "urea_nitrogen": "mmol/L", "albumin": "g/L", "hemoglobin": "g/L",
    "energy": "kcal", "protein": "g", "weight": "kg", "height": "cm",
}

# 队列排布：(分期, 透析方式, 年龄带) —— 显式列出而非笛卡尔积，保证临床可辩护
# 透析主要落在 G5；G4 仅保留 2 例难治性容量负荷提前起始透析；幼儿透析以 PD 为主
COHORT: list[tuple[str, str, str]] = [
    ("G2", "none", "toddler"), ("G3a", "none", "toddler"), ("G3b", "none", "toddler"),
    ("G4", "none", "toddler"), ("G5", "none", "toddler"), ("G5", "PD", "toddler"),
    ("G5", "PD", "toddler"),
    ("G2", "none", "preschool"), ("G3a", "none", "preschool"), ("G3b", "none", "preschool"),
    ("G4", "none", "preschool"), ("G5", "none", "preschool"), ("G5", "PD", "preschool"),
    ("G5", "HD", "preschool"),
    ("G2", "none", "school"), ("G3a", "none", "school"), ("G3b", "none", "school"),
    ("G4", "none", "school"), ("G4", "PD", "school"), ("G5", "none", "school"),
    ("G5", "HD", "school"), ("G5", "PD", "school"), ("G3a", "none", "school"),
    ("G3b", "none", "school"),
    ("G2", "none", "adolescent"), ("G3a", "none", "adolescent"), ("G3b", "none", "adolescent"),
    ("G4", "none", "adolescent"), ("G4", "HD", "adolescent"), ("G5", "none", "adolescent"),
    ("G5", "HD", "adolescent"), ("G5", "PD", "adolescent"), ("G5", "HD", "adolescent"),
    ("G2", "none", "adolescent"), ("G3b", "none", "adolescent"), ("G3a", "none", "adolescent"),
]
