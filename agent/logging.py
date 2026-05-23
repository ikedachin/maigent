import logging
import os


class AgentColorFormatter(logging.Formatter):
    COLORS = {
        "plan": "\033[95m",
        "rag": "\033[96m",
        "sandbox": "\033[93m",
        "llm": "\033[92m",
        "web": "\033[94m",
        "message": "\033[90m",
        "error": "\033[91m",
        "reset": "\033[0m",
    }

    LABELS = {
        "plan": "PLAN",
        "rag": "RAG",
        "sandbox": "SANDBOX",
        "llm": "LLM",
        "web": "WEB",
        "message": "MSG",
        "error": "ERROR",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = not os.environ.get("NO_COLOR")

    def format(self, record: logging.LogRecord) -> str:
        category = self._category(record)
        record.tool = self.LABELS[category]
        line = super().format(record)
        if not self.use_color:
            return line
        return f"{self.COLORS[category]}{line}{self.COLORS['reset']}"

    def _category(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR:
            return "error"
        message = record.getMessage()
        if "agent_error" in message or "status=failed" in message or "sandbox_error" in message:
            return "error"
        if message.startswith(("rag_", "bm25_")) or " tool=rag" in message:
            return "rag"
        if message.startswith("sandbox_") or " tool=sandbox" in message:
            return "sandbox"
        if message.startswith("llm_"):
            return "llm"
        if message.startswith(("tool_plan", "agent_plan", "agent_step_")):
            return "plan"
        if "web_search" in message:
            return "web"
        if message.startswith("message_received"):
            return "message"
        return "message"
