from core.interfaces import ModelPlugin, PluginInfo
from business.model_client import stream_chat, stream_chat_with_tools, chat_sync

class ModelScopeModelPlugin(ModelPlugin):
    def load(self, plugin_info: PluginInfo):
        self._info = plugin_info
        print(f"[ModelPlugin] 加载 {plugin_info.name}")

    def unload(self):
        print(f"[ModelPlugin] 卸载 {self._info.name}")

    def stream_chat(self, messages, **kwargs):
        yield from stream_chat(messages, **kwargs)

    def stream_chat_with_tools(self, messages, tools, **kwargs):
        yield from stream_chat_with_tools(messages, tools, **kwargs)

    def sync_chat(self, messages, **kwargs):
        return chat_sync(messages, **kwargs)