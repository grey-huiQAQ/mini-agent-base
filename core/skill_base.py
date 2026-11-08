"""
Skill 基类定义
所有技能必须继承此基类并实现抽象方法
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillManifest:
    """技能清单数据类，对应 manifest.json"""
    name: str                    # 技能唯一标识
    display_name: str            # 显示名称
    description: str             # 技能描述
    version: str = "1.0.0"       # 版本号
    author: str = ""             # 作者
    tags: List[str] = field(default_factory=list)  # 标签
    input_schema: Dict[str, Any] = field(default_factory=dict)  # 输入参数 schema
    output_schema: Dict[str, Any] = field(default_factory=dict)  # 输出参数 schema


class Skill(ABC):
    """技能基类，所有技能必须继承此类"""

    def __init__(self):
        self._manifest: Optional[SkillManifest] = None

    @property
    def manifest(self) -> Optional[SkillManifest]:
        return self._manifest

    @manifest.setter
    def manifest(self, value: SkillManifest):
        self._manifest = value

    @property
    def name(self) -> str:
        return self._manifest.name if self._manifest else "unknown"

    @property
    def display_name(self) -> str:
        return self._manifest.display_name if self._manifest else "未知技能"

    @property
    def description(self) -> str:
        return self._manifest.description if self._manifest else ""

    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行技能

        Args:
            context: 执行上下文，包含技能所需的输入参数

        Returns:
            执行结果字典
        """
        pass

    def validate_input(self, context: Dict[str, Any]) -> bool:
        """
        验证输入参数是否符合 schema（可选重写）

        Args:
            context: 输入上下文

        Returns:
            是否有效
        """
        return True

    def on_load(self):
        """技能加载时的初始化钩子（可选重写）"""
        pass

    def on_unload(self):
        """技能卸载时的清理钩子（可选重写）"""
        pass
