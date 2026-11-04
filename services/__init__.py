"""纯逻辑服务层：平台公式复刻、回归拟合、估价预计算。

不 import astrbot、不发网络请求、不直接持有连接（连接由调用方传入或纯函数出参）。
设计依据：docs/astrbot-league-advisor-design.md §5.6（公式复刻 + 黄金用例）、
§5.7（预计算 vs 现场计算，C 混合裁决）。
"""
