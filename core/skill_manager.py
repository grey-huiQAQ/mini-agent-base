"""
SkillManager - 技能管理器
负责技能的注册、加载、查询和执行
"""
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .skill_base import Skill, SkillManifest


class SkillManager:
    """技能管理器，管理所有已注册的技能"""

    def __init__(self, skills_dir: str = "skills"):
        self._skills: Dict[str, Skill] = {}
        self._manifests: Dict[str, SkillManifest] = {}
        self._skills_dir = Path(skills_dir)

    @property
    def skills_dir(self) -> Path:
        return self._skills_dir

    def load_skill_from_dir(self, skill_dir: Path) -> bool:
        """
        从目录加载单个技能

        Args:
            skill_dir: 技能目录路径

        Returns:
            是否加载成功
        """
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"[SkillManager] 未找到 manifest.json: {skill_dir}")
            return False

        try:
            # 读取技能清单
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
            manifest = SkillManifest(**manifest_data)

            # 动态导入技能模块
            if str(skill_dir.parent) not in sys.path:
                sys.path.insert(0, str(skill_dir.parent))

            module_name = f"{skill_dir.name}.skill"
            module = importlib.import_module(module_name)
            skill_class = getattr(module, "SkillImpl")
            skill_instance = skill_class()
            skill_instance.manifest = manifest

            # 调用加载钩子
            skill_instance.on_load()

            # 注册技能
            self._skills[manifest.name] = skill_instance
            self._manifests[manifest.name] = manifest

            print(f"[SkillManager] 加载技能: {manifest.display_name} v{manifest.version}")
            return True

        except Exception as e:
            print(f"[SkillManager] 加载技能失败 {skill_dir}: {e}")
            return False

    def load_all_skills(self) -> int:
        """
        从 skills 目录加载所有技能

        Returns:
            成功加载的技能数量
        """
        if not self._skills_dir.exists():
            print(f"[SkillManager] 技能目录不存在: {self._skills_dir}")
            return 0

        count = 0
        for item in self._skills_dir.iterdir():
            if item.is_dir() and (item / "manifest.json").exists():
                if self.load_skill_from_dir(item):
                    count += 1

        print(f"[SkillManager] 共加载 {count} 个技能")
        return count

    def get_skill(self, name: str) -> Optional[Skill]:
        """获取指定名称的技能"""
        return self._skills.get(name)

    def get_manifest(self, name: str) -> Optional[SkillManifest]:
        """获取指定技能的清单"""
        return self._manifests.get(name)

    def list_skills(self) -> List[Dict[str, str]]:
        """
        列出所有已加载技能的基本信息

        Returns:
            技能信息列表
        """
        result = []
        for name, manifest in self._manifests.items():
            result.append({
                "name": name,
                "display_name": manifest.display_name,
                "description": manifest.description,
                "version": manifest.version,
                "tags": ", ".join(manifest.tags) if manifest.tags else "",
            })
        return result

    def run_skill(self, skill_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行指定技能

        Args:
            skill_name: 技能名称
            context: 执行上下文

        Returns:
            执行结果
        """
        skill = self.get_skill(skill_name)
        if not skill:
            return {"error": f"技能 '{skill_name}' 不存在"}

        # 验证输入
        if not skill.validate_input(context):
            return {"error": f"技能 '{skill_name}' 输入参数验证失败"}

        try:
            result = skill.execute(context)
            return result
        except Exception as e:
            print(f"[SkillManager] 执行技能 '{skill_name}' 失败: {e}")
            return {"error": f"执行失败: {str(e)}"}
