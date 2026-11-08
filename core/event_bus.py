"""
事件总线 - 发布/订阅模式，用于模块间解耦

备注 (2026-09-06)：生命周期事件 "agent_started"/"agent_stopped" 目前有发布者（core/runtime.py）但零订阅者，属预留事件，等待接线（如运行审计、自动恢复等）。
"""
from typing import Callable, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Event:
    event_type: str
    data: Dict[str, Any]
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Event], None]]] = {}

    def subscribe(self, event_type: str, handler: Callable[[Event], None]):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def publish(self, event: Event):
        handlers = self._subscribers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                print(f"[EventBus] Error in handler for {event.event_type}: {e}")

    def unsubscribe(self, event_type: str, handler: Callable[[Event], None]):
        if event_type in self._subscribers and handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)