# -*- coding: utf-8 -*-
"""
ui —— Gradio 界面层包（2026-09-05 拆分骨架，V1）

当前结构：
- app_factory.py：create_app() 工厂，承载原 app.py 的 UI 构建块（组件 / 布局 / 事件绑定）；
- components/、pages/、controllers/：预留「组件 / 页面 / 控制器」三层拆分的空骨架。 (2026-09-12)

约束背景：回调函数与模块级单例仍定义于 app.py（测试依赖 app 模块命名空间，
详见 ui/app_factory.py 模块 docstring），本包各子模块为后续拆分预留。 (2026-09-12)
"""
