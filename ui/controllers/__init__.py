# -*- coding: utf-8 -*-
"""
ui.controllers —— 控制器层（拆分骨架 V1，空占位）

后续规划：承接事件回调（现定义于 app.py）的迁移。回调迁出 app 模块前，
需先解除测试对 app 命名空间的依赖（patch("app.*") / from app import ...），
否则全量回归将失败（详见 ui/app_factory.py 模块 docstring）；
本阶段不承载实际代码。
"""
