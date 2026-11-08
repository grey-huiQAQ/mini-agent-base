"""
核心接口定义

关于"插件类型"：曾有一个 `PluginType(Enum)`（MODEL/TOOL/MEMORY/SKILL/SCHEDULER），
**已在 P5-4 删除**——理由是全仓只出现在它自己的定义处（零引用），而插件类型实际
一直以**字符串**传递（见 `PluginInfo.type` 的注释与 `plugin_manager.get_plugins_by_type()`）。
留着一个没人用的枚举等于给"类型"这件事造了第二个假权威。
同理删除了 `MemoryPlugin`（同为零引用的"设计了没接线"产物）。
**没有**把 `PluginInfo.type` 改成枚举：那会把一条字符串契约改成枚举契约，牵连
`plugins/*/manifest` 与两个调用点，属于用新耦合修旧耦合。
（该导入在删除后已无使用者，**本批按"只做删除与指向"的范围把它一并删掉**——
留一个没有任何使用者的 `from enum import Enum` 只会让下一个人以为"枚举还要回来的"。
删除理由与裁决过程见 `PROJECT_STATUS.md` §五.27「删掉'假权威'比补一个更好的权威
更划算」：**这里不写"见 commit message"**，因为 commit message 不是持久来源——
它不在工作区里，读者无法在阅读代码的同一处核对，而文档章节可以。）

关于两套加载器：`SkillManager` 与 `PluginManager` **已裁决不合并**（P5-4）——
两者契约不同且都在被用：plugins 由 `core/runtime.py` 与 `ui/controllers/_shared.py`
使用，skills 按类名反射加载 `SkillImpl`；合并是纯结构改动、零用户价值。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Generator


@dataclass
class PluginInfo:
    name: str
    version: str
    type: str          # 使用字符串，避免 Enum 序列化问题
    entry_point: str
    dependencies: List[str] = None
    config: Dict[str, Any] = None


class Plugin(ABC):
    @abstractmethod
    def load(self, plugin_info: PluginInfo):
        pass

    @abstractmethod
    def unload(self):
        pass


class ModelPlugin(Plugin):
    @abstractmethod
    def stream_chat(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        pass

    @abstractmethod
    def stream_chat_with_tools(self, messages: List[Dict[str, str]], tools: List[Dict], **kwargs) -> Generator[str, None, None]:
        pass

    @abstractmethod
    def sync_chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        pass


class ToolPlugin(Plugin):
    @abstractmethod
    def get_tools(self) -> List[Dict[str, Any]]:
        """返回该插件提供的所有工具 schema 列表"""
        pass

    @abstractmethod
    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        pass


class ConversationStore(ABC):
    @abstractmethod
    def create_session(self, title: str = "") -> str:
        pass

    @abstractmethod
    def save_message(self, session_id: str, role: str, content: str):
        pass

    @abstractmethod
    def get_messages(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
        pass

    @abstractmethod
    def get_session(self, session_id: str) -> Dict:
        pass

    @abstractmethod
    def delete_session(self, session_id: str):
        pass

    @abstractmethod
    def list_sessions(self) -> List[Dict]:
        pass