from agents.langchain_agent import LangChainAgent
from agents.registry import register


@register
class Carmen(LangChainAgent):
    id = "carmen"
    name = "Carmen"
    description = "Friendly general-purpose assistant"
    system_prompt = "You are Carmen, a friendly and concise assistant. Answer clearly and helpfully."
