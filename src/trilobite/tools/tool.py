from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Tool(ABC):
    name: str
    description: str
    parameters: dict

    @abstractmethod
    def execute(self, working_dir: Path, session_dir: Path, **kwargs: Any) -> str:
        ...

    def to_openai_tool(self, enable_vl: bool = False) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
