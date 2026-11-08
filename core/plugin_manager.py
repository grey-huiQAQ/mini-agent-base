import importlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional
from .interfaces import Plugin, PluginInfo


class PluginManager:
    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}
        self._plugin_infos: Dict[str, PluginInfo] = {}

    def load_plugin_from_dir(self, plugin_dir: Path):
        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"[PluginManager] 未找到 manifest.json: {plugin_dir}")
            return
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            info = PluginInfo(**manifest)
            module_name, class_name = info.entry_point.split(".")
            # 将 plugins 目录加入 sys.path，以便导入插件包
            plugins_root = plugin_dir.parent
            if str(plugins_root) not in sys.path:
                sys.path.insert(0, str(plugins_root))
            module = importlib.import_module(f"{plugin_dir.name}.{module_name}")
            plugin_class = getattr(module, class_name)
            plugin_instance = plugin_class()
            plugin_instance.load(info)
            self._plugins[info.name] = plugin_instance
            self._plugin_infos[info.name] = info
            print(f"[PluginManager] 加载插件: {info.name} v{info.version}")
        except Exception as e:
            print(f"[PluginManager] 加载插件失败 {plugin_dir}: {e}")

    def load_all_plugins(self, plugins_dir="plugins") -> int:
        """扫描 plugins_dir 下所有含 manifest.json 的子目录并逐个加载，返回成功加载数

        与 SkillManager.load_all_skills() 同形状：**加一个插件 = 丢一个目录**，不需要
        改任何代码。此前两个内置插件是在 ui/controllers/_shared.py 里以硬编码路径加载的，
        意味着"plugins/" 实际并不是扩展点、而是带清单的内部接线——本方法把两个扩展面
        （skills/ 与 plugins/）拉成同一种用法。

        计数方式用注册表增量而不是修改 load_plugin_from_dir 的返回值：后者返回 None 是
        既有约定（加载失败只打印不抛），改签名会波及调用方与既有断言。

        Args:
            plugins_dir: 插件根目录（默认 "plugins"）

        Returns:
            成功加载的插件数量
        """
        base = Path(plugins_dir)
        if not base.is_dir():
            print(f"[PluginManager] 插件目录不存在: {base}")
            return 0

        count = 0
        for item in sorted(base.iterdir()):
            if item.is_dir() and (item / "manifest.json").exists():
                before = len(self._plugins)
                self.load_plugin_from_dir(item)
                if len(self._plugins) > before:
                    count += 1

        print(f"[PluginManager] 共加载 {count} 个插件")
        return count

    def get_plugin(self, name: str) -> Optional[Plugin]:
        return self._plugins.get(name)

    def get_plugins_by_type(self, plugin_type: str) -> List[Plugin]:
        return [p for name, p in self._plugins.items() if self._plugin_infos[name].type == plugin_type]